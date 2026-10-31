import json
from datetime import timedelta

import pytest
from conftest import FIXED_NOW
from fakes import profile_reply
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    AiCall,
    Game,
    Profile,
    ProfileVersion,
    ProfileVersionSource,
    ReviewVersion,
)
from app.services.people import add_group, rename_group
from app.services.profiles import (
    SECTION_LABELS,
    TAG_LABELS,
    ProfileContent,
    ProfileLine,
    ProfileTarget,
    ProfileUpdate,
    current_version,
    ensure_profile,
    group_key,
    lane_key,
    list_profiles,
    play_history,
    profile_sources,
    review_input,
    save_version,
    targets_with_reviews,
    unprocessed_versions,
    validate_update,
)
from app.services.reviews import Lane, ReviewForm, publish, save_draft

# --- helpers ----------------------------------------------------------------


def ok_update(*, reviews=(), games=(), changelog: str = "Learned from a review."):
    """An update with one valid stated line citing the given reviews and games."""
    return ProfileUpdate.model_validate(
        {
            "profile": {
                "loved": [
                    {
                        "text": "Likes a cozy co-op night",
                        "tag": "stated",
                        "games": list(games),
                        "reviews": list(reviews),
                    }
                ]
            },
            "changelog": changelog,
        }
    )


def fake_call(
    db, *, model: str = "sample-big", prompt_version: str = "abc123"
) -> AiCall:
    call = AiCall(
        job="recommender",
        provider_kind="anthropic",
        model=model,
        prompt_name="profile_update",
        prompt_version=prompt_version,
        prompt_hash="0" * 64,
        request="prompt",
        response="reply",
        duration_ms=5,
        created_at=FIXED_NOW,
    )
    db.add(call)
    db.commit()
    return call


def lane_target(lane: Lane) -> ProfileTarget:
    return ProfileTarget(lane_key(lane), "lane", lane, None, lane.value)


def solo_profile(db):
    return ensure_profile(db, lane_target(Lane.SOLO), FIXED_NOW)


def learn(db, profile, versions, **kwargs):
    """Save a profile version that learned from `versions`."""
    return save_version(
        db,
        profile,
        ok_update(reviews=[v.id for v in versions]),
        source_ids=[v.id for v in versions],
        call=fake_call(db),
        refresh_run_id=kwargs.get("refresh_run_id"),
        now=kwargs.get("now", FIXED_NOW),
    )


def count_queries(app, fn):
    """(what `fn()` returned, how many SQL statements it ran)."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", record)
    try:
        result = fn()
    finally:
        event.remove(app.state.engine, "before_cursor_execute", record)
    return result, len(seen)


def loaded_versions(db) -> list[ReviewVersion]:
    """Every review version, freshly queried (as `unprocessed_versions` returns them)."""
    db.expire_all()
    return list(db.scalars(select(ReviewVersion).order_by(ReviewVersion.id)))


# --- the brief's tests ------------------------------------------------------


def test_targets_follow_published_versions(db, games, people):
    sam, alex, crew = people
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="draft only"), FIXED_NOW)
    publish(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(rating=9, named_group_id=crew.id, text="fun"),
        FIXED_NOW,
    )
    assert [t.key for t in targets_with_reviews(db)] == [
        "lane:three_four",
        f"group:{crew.id}",
    ]


def test_unprocessed_versions_oldest_first_and_once(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(
        db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW + timedelta(minutes=1)
    )
    profile = ensure_profile(db, targets_with_reviews(db)[0], FIXED_NOW)
    assert [v.id for v in unprocessed_versions(db, profile)] == [v1.id, v2.id]
    save_version(
        db,
        profile,
        ok_update(reviews=[v1.id], games=[10]),
        source_ids=[v1.id],
        call=fake_call(db),
        refresh_run_id=None,
        now=FIXED_NOW,
    )
    assert [v.id for v in unprocessed_versions(db, profile)] == [v2.id]


def test_review_input_is_verbatim(db, games):
    raw = "  <b>Loved</b> it 🎲\n\nsecond  "
    v = publish(db, 10, Lane.SOLO, ReviewForm(text=raw, rating=8), FIXED_NOW)
    [item] = review_input(db, [v])
    assert item["text"] == raw and item["ref"] == f"R{v.id}"
    assert item["game"] == "Sample Quest"


@pytest.mark.parametrize(
    "line,section,problem",
    [  # Review focus 4
        (
            {"text": "x", "tag": "stated", "reviews": []},
            "loved",
            "What you love: a stated line must cite a review it learned from.",
        ),
        (
            {"text": "x", "tag": "observed", "games": [10]},
            "patterns",
            "Patterns: an observed line must name at least two reviewed games.",
        ),
        (
            {"text": "x", "tag": "stated", "reviews": [1]},
            "hunches",
            "Hunches to test: every line must be a guess.",
        ),
        (
            {"text": "x", "tag": "guess", "games": [999]},
            "hunches",
            "Hunches to test: a line cites a game that isn't in your reviews.",
        ),
        (
            {"text": "x", "tag": "stated", "reviews": [77]},
            "loved",
            "What you love: a line cites a review it didn't learn from.",
        ),
    ],
)
def test_validate_update_rules(line, section, problem):
    update = ProfileUpdate.model_validate(
        {"profile": {section: [line]}, "changelog": "c"}
    )
    assert problem in validate_update(
        update, review_ids={1}, reviewed_appids={10, 20}, history_appids={30}
    )


def test_guess_may_cite_play_history():
    update = ProfileUpdate.model_validate(
        {
            "profile": {"hunches": [{"text": "x", "tag": "guess", "games": [30]}]},
            "changelog": "c",
        }
    )
    assert (
        validate_update(
            update, review_ids=set(), reviewed_appids=set(), history_appids={30}
        )
        == []
    )


def test_list_profiles_always_shows_four_lanes(db, people):
    keys = [s.target.key for s in list_profiles(db)]
    assert keys[:4] == ["lane:solo", "lane:two", "lane:three_four", "lane:five_plus"]
    assert keys[4:] == [f"group:{people[2].id}"]


# --- schema, labels and keys --------------------------------------------------


def test_labels_and_keys():
    assert SECTION_LABELS == {
        "loved": "What you love",
        "dealbreakers": "Dealbreakers",
        "patterns": "Patterns",
        "hunches": "Hunches to test",
        "open_questions": "Open questions",
    }
    assert TAG_LABELS == {"stated": "Stated", "observed": "Observed", "guess": "Guess"}
    assert lane_key(Lane.FIVE_PLUS) == "lane:five_plus"
    assert lane_key("two") == "lane:two"
    assert group_key(3) == "group:3"


def test_content_defaults_to_empty_sections():
    update = ProfileUpdate.model_validate({"profile": {}, "changelog": "c"})
    assert update.profile.model_dump() == {
        "loved": [],
        "dealbreakers": [],
        "patterns": [],
        "hunches": [],
        "open_questions": [],
    }
    line = ProfileLine(text="x", tag="guess")
    assert line.games == [] and line.reviews == []


def test_schema_limits():
    def line(**changes):
        return {"text": "x", "tag": "guess", **changes}

    def bad(profile=None, changelog="c"):
        with pytest.raises(ValidationError):
            ProfileUpdate.model_validate(
                {"profile": profile or {}, "changelog": changelog}
            )

    bad({"loved": [line(text="")]})
    bad({"loved": [line(text="x" * 401)]})
    bad({"loved": [line(tag="loved")]})
    bad({"loved": [line()] * 16})
    bad({"open_questions": ["q"] * 11})
    bad(changelog="")
    bad(changelog="x" * 301)
    ProfileUpdate.model_validate(
        {
            "profile": {
                "loved": [line(text="x" * 400)] * 15,
                "open_questions": ["q"] * 10,
            },
            "changelog": "x" * 300,
        }
    )


def test_profile_reply_builds_a_parsable_update():
    reply = profile_reply(
        loved=[{"text": "x", "tag": "stated", "games": [10], "reviews": [1]}]
    )
    update = ProfileUpdate.model_validate(json.loads(reply))
    assert update.changelog == "Learned from new reviews."
    assert update.profile.loved[0].games == [10]
    assert update.profile.hunches == [] and update.profile.open_questions == []
    assert (
        ProfileUpdate.model_validate(
            json.loads(profile_reply("Changed it.", open_questions=["Why?"]))
        ).changelog
        == "Changed it."
    )
    with pytest.raises(TypeError):
        profile_reply(loves=[])


# --- targets and profiles -------------------------------------------------------


def test_targets_order_lanes_then_groups_by_name(db, games, people):
    sam, alex, crew = people
    arcade = add_group(db, "arcade pals", [sam.id], FIXED_NOW)
    publish(
        db,
        10,
        Lane.FIVE_PLUS,
        ReviewForm(text="a", named_group_id=crew.id),
        FIXED_NOW,
    )
    publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(text="b", named_group_id=arcade.id),
        FIXED_NOW,
    )
    publish(db, 20, Lane.SOLO, ReviewForm(text="c"), FIXED_NOW)
    targets = targets_with_reviews(db)
    assert [t.key for t in targets] == [
        "lane:solo",
        "lane:two",
        "lane:five_plus",
        f"group:{arcade.id}",  # "arcade pals" before "Sample Crew", ignoring case
        f"group:{crew.id}",
    ]
    assert [t.label for t in targets] == [
        "Solo",
        "Two players",
        "Five or more",
        "arcade pals",
        "Sample Crew",
    ]
    solo, group = targets[0], targets[-1]
    assert (solo.kind, solo.lane, solo.group_id) == ("lane", Lane.SOLO, None)
    assert (group.kind, group.lane, group.group_id) == ("group", None, crew.id)


def test_targets_empty_until_something_is_published(db, games, people):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="draft"), FIXED_NOW)
    assert targets_with_reviews(db) == []


def test_a_group_target_lists_each_group_once_and_uses_its_current_name(
    db, games, people
):
    sam, alex, crew = people
    for appid in (10, 20):
        publish(
            db,
            appid,
            Lane.TWO,
            ReviewForm(text="x", named_group_id=crew.id),
            FIXED_NOW,
        )
    rename_group(db, crew.id, "Renamed Crew")
    group_targets = [t for t in targets_with_reviews(db) if t.kind == "group"]
    assert [(t.key, t.label) for t in group_targets] == [
        (f"group:{crew.id}", "Renamed Crew")
    ]


def test_ensure_profile_creates_once(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.TWO, ReviewForm(text="x", named_group_id=crew.id), FIXED_NOW)
    lane, group = targets_with_reviews(db)
    first = ensure_profile(db, lane, FIXED_NOW)
    again = ensure_profile(db, lane, FIXED_NOW + timedelta(days=1))
    assert first.id == again.id
    assert again.created_at == FIXED_NOW
    assert (first.key, first.kind, first.lane, first.group_id) == (
        "lane:two",
        "lane",
        "two",
        None,
    )
    for_group = ensure_profile(db, group, FIXED_NOW)
    assert (for_group.key, for_group.kind, for_group.lane, for_group.group_id) == (
        f"group:{crew.id}",
        "group",
        None,
        crew.id,
    )
    assert db.scalar(select(func.count()).select_from(Profile)) == 2


def test_profile_keys_are_unique(db):
    db.add(Profile(key="lane:solo", kind="lane", lane="solo", created_at=FIXED_NOW))
    db.commit()
    db.add(Profile(key="lane:solo", kind="lane", lane="solo", created_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()


def test_profile_version_numbers_are_unique_within_a_profile(db):
    profile = solo_profile(db)
    call = fake_call(db)

    def row(number: int) -> ProfileVersion:
        return ProfileVersion(
            profile_id=profile.id,
            version=number,
            model="m",
            provider_kind="local",
            prompt_name="p",
            prompt_version="v",
            created_at=FIXED_NOW,
            ai_call_id=call.id,
        )

    db.add_all([row(1), row(2)])
    db.commit()
    assert (
        db.scalar(select(ProfileVersion.content).where(ProfileVersion.version == 1))
        == {}
    )
    assert db.scalar(select(ProfileVersion.changelog).limit(1)) == ""
    db.add(row(2))
    with pytest.raises(IntegrityError):
        db.commit()


# --- unprocessed versions and sources -------------------------------------------


def test_unprocessed_versions_belong_to_the_profiles_lane(db, games):
    solo = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    assert [v.id for v in unprocessed_versions(db, profile)] == [solo.id]


def test_unprocessed_versions_of_a_group_profile_span_lanes(db, games, people):
    sam, alex, crew = people
    tagged_two = publish(
        db, 10, Lane.TWO, ReviewForm(text="a", named_group_id=crew.id), FIXED_NOW
    )
    tagged_five = publish(
        db,
        20,
        Lane.FIVE_PLUS,
        ReviewForm(text="b", named_group_id=crew.id),
        FIXED_NOW + timedelta(minutes=1),
    )
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(text="untagged"), FIXED_NOW)
    target = next(t for t in targets_with_reviews(db) if t.kind == "group")
    profile = ensure_profile(db, target, FIXED_NOW)
    assert [v.id for v in unprocessed_versions(db, profile)] == [
        tagged_two.id,
        tagged_five.id,
    ]


def test_unprocessed_versions_include_every_version_of_a_review(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="first"), FIXED_NOW)
    v2 = publish(
        db, 10, Lane.SOLO, ReviewForm(text="second"), FIXED_NOW + timedelta(minutes=5)
    )
    profile = solo_profile(db)
    learn(db, profile, [v1])
    assert [v.id for v in unprocessed_versions(db, profile)] == [v2.id]


def test_unprocessed_versions_tie_on_time_goes_by_id_and_limit_keeps_the_oldest(
    db, games
):
    made = [
        publish(db, appid, lane, ReviewForm(text="x"), FIXED_NOW)
        for appid in (10, 20)
        for lane in (Lane.SOLO,)
    ]
    # One more, older than the rest but saved last (a higher id).
    older = publish(
        db, 10, Lane.SOLO, ReviewForm(text="old"), FIXED_NOW - timedelta(days=1)
    )
    profile = solo_profile(db)
    ids = [v.id for v in unprocessed_versions(db, profile)]
    assert ids == [older.id, made[0].id, made[1].id]
    assert [v.id for v in unprocessed_versions(db, profile, limit=2)] == ids[:2]


def test_unprocessed_versions_ignore_what_other_profiles_learned(db, games, people):
    sam, alex, crew = people
    both = publish(
        db, 10, Lane.TWO, ReviewForm(text="a", named_group_id=crew.id), FIXED_NOW
    )
    lane_target_two, group_target = targets_with_reviews(db)
    lane_profile = ensure_profile(db, lane_target_two, FIXED_NOW)
    group_profile = ensure_profile(db, group_target, FIXED_NOW)
    learn(db, lane_profile, [both])
    assert unprocessed_versions(db, lane_profile) == []
    assert [v.id for v in unprocessed_versions(db, group_profile)] == [both.id]


def test_unprocessed_versions_is_a_fixed_number_of_queries(app, db, games):
    profile = solo_profile(db)
    for appid in (10, 20):
        publish(db, appid, Lane.SOLO, ReviewForm(text="x"), FIXED_NOW)
    _, few = count_queries(app, lambda: unprocessed_versions(db, profile))
    for number in range(6):
        publish(
            db,
            10,
            Lane.SOLO,
            ReviewForm(text=f"more {number}"),
            FIXED_NOW + timedelta(minutes=number),
        )
    result, many = count_queries(app, lambda: unprocessed_versions(db, profile))
    assert len(result) == 8 and many == few


def test_profile_sources(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 10, Lane.SOLO, ReviewForm(text="a2"), FIXED_NOW)
    v3 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    assert profile_sources(db, profile) == (set(), set())
    learn(db, profile, [v1])
    learn(db, profile, [v2, v3])
    assert profile_sources(db, profile) == ({v1.id, v2.id, v3.id}, {10, 20})


def test_profile_sources_only_counts_this_profile(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    solo = solo_profile(db)
    two = ensure_profile(db, lane_target(Lane.TWO), FIXED_NOW)
    learn(db, solo, [v1])
    learn(db, two, [v2])
    assert profile_sources(db, solo) == ({v1.id}, {10})
    assert profile_sources(db, two) == ({v2.id}, {20})


def test_profile_sources_is_one_query(app, db, games):
    versions = [
        publish(db, appid, Lane.SOLO, ReviewForm(text="x"), FIXED_NOW)
        for appid in (10, 20)
    ]
    profile = solo_profile(db)
    learn(db, profile, versions)
    db.refresh(profile)  # the commit expired it
    _, queries = count_queries(app, lambda: profile_sources(db, profile))
    assert queries == 1


def test_current_version(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    assert current_version(db, profile) is None
    first = learn(db, profile, [v1])
    assert current_version(db, profile).id == first.id
    second = learn(db, profile, [v2])
    assert current_version(db, profile).id == second.id
    other = ensure_profile(db, lane_target(Lane.TWO), FIXED_NOW)
    assert current_version(db, other) is None


# --- review_input ---------------------------------------------------------------


def test_review_input_fields(db, games, people):
    sam, alex, crew = people
    first = publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(rating=6, verdict="mixed", text="meh"),
        FIXED_NOW,
    )
    second = publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(
            rating=9,
            verdict="loved",
            status="finished",
            walk_away="no",
            session_length="about two hours",
            complexity="about_right",
            tags_raw="Co-op, Cozy",
            text="Much better now",
            group_text="We laughed a lot",
            people=[sam.id],
            named_group_id=crew.id,
        ),
        FIXED_NOW + timedelta(days=3),
    )
    [item] = review_input(db, loaded_versions(db)[1:])
    assert item == {
        "id": second.id,
        "ref": f"R{second.id}",
        "appid": 10,
        "game": "Sample Quest",
        "lane": "Two players",
        "version": 2,
        "published": "2026-01-18",
        "rating": 9,
        "verdict": "loved",
        "status": "finished",
        "walk_away": "no",
        "session_length": "about two hours",
        "complexity": "about_right",
        "tags": ["co-op", "cozy"],
        "played_with": ["Alex", "Sam"],
        "group": "Sample Crew",
        "text": "Much better now",
        "group_text": "We laughed a lot",
        "steam_hours": 10.0,
        "earlier": {"rating": 6, "verdict": "mixed"},
    }
    [earliest] = review_input(db, loaded_versions(db)[:1])
    assert earliest["id"] == first.id and earliest["earlier"] is None
    assert earliest["played_with"] == [] and earliest["group"] is None


def test_review_input_earlier_is_the_previous_version_of_the_same_review(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=3, verdict="disliked"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5, verdict="mixed"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=1, verdict="disliked"), FIXED_NOW)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=10, verdict="loved"), FIXED_NOW)
    items = review_input(db, loaded_versions(db))
    assert [(i["appid"], i["lane"], i["version"], i["earlier"]) for i in items] == [
        (10, "Solo", 1, None),
        (10, "Solo", 2, {"rating": 3, "verdict": "disliked"}),
        (10, "Solo", 3, {"rating": 5, "verdict": "mixed"}),
        (10, "Two players", 1, None),
        (20, "Solo", 1, None),
    ]


def test_review_input_keeps_the_order_it_is_given(db, games):
    a = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    b = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    assert [i["id"] for i in review_input(db, [b, a])] == [b.id, a.id]
    assert review_input(db, []) == []


def test_review_input_leaves_user_text_exactly_as_stored(db, games, people):
    sam, alex, crew = people
    text = (
        "\n\t  Trailing and leading space  \r\n{{ not_a_template }} <script>x</script> "
    )
    group_text = "  group words \n"
    v = publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(
            text=text, group_text=group_text, session_length="  1h ", people=[sam.id]
        ),
        FIXED_NOW,
    )
    [item] = review_input(db, [v])
    assert item["text"] == text and item["group_text"] == group_text
    assert item["session_length"] == "  1h "
    stored = db.get(ReviewVersion, v.id)
    assert (stored.text, stored.group_text) == (text, group_text)


def test_review_input_uses_the_groups_current_name(db, games, people):
    sam, alex, crew = people
    v = publish(
        db, 10, Lane.TWO, ReviewForm(text="x", named_group_id=crew.id), FIXED_NOW
    )
    rename_group(db, crew.id, "Renamed Crew")
    [item] = review_input(db, loaded_versions(db))
    assert item["group"] == "Renamed Crew" and item["id"] == v.id
    assert item["played_with"] == ["Alex", "Sam"]


def test_review_input_is_a_fixed_number_of_queries(app, db, games, people):
    sam, alex, crew = people
    for appid in (10, 20):
        for lane in (Lane.TWO, Lane.THREE_FOUR, Lane.FIVE_PLUS):
            form = ReviewForm(text="x", people=[sam.id], named_group_id=crew.id)
            publish(db, appid, lane, form, FIXED_NOW)
            publish(db, appid, lane, form, FIXED_NOW + timedelta(days=1))
    versions = loaded_versions(db)
    assert len(versions) == 12
    few, queries_few = count_queries(app, lambda: review_input(db, versions[:2]))
    many, queries_many = count_queries(app, lambda: review_input(db, versions))
    assert (len(few), len(many)) == (2, 12)
    assert queries_many == queries_few
    assert queries_many <= 6


# --- play history ---------------------------------------------------------------


def test_play_history(db, games):
    db.add_all(
        [
            Game(
                appid=30,
                name="Demo Dungeon",
                owned=True,
                playtime_minutes=1234,
                last_played_at=None,
                source="steam_library",
                created_at=FIXED_NOW,
            ),
            Game(
                appid=40,
                name="Unplayed",
                owned=True,
                playtime_minutes=0,
                source="steam_library",
                created_at=FIXED_NOW,
            ),
            Game(
                appid=50,
                name="Never synced",
                owned=True,
                playtime_minutes=None,
                source="steam_library",
                created_at=FIXED_NOW,
            ),
            Game(
                appid=60,
                name="Not owned",
                owned=False,
                playtime_minutes=999,
                source="store_search",
                created_at=FIXED_NOW,
            ),
        ]
    )
    db.commit()
    assert play_history(db) == [
        {"appid": 30, "game": "Demo Dungeon", "hours": 20.6, "last_played": None},
        {
            "appid": 10,
            "game": "Sample Quest",
            "hours": 10.0,
            "last_played": "2026-01-14",
        },
        {
            "appid": 20,
            "game": "Test Tactics",
            "hours": 2.0,
            "last_played": "2026-01-13",
        },
    ]
    assert [g["appid"] for g in play_history(db, limit=2)] == [30, 10]


# --- validate_update ------------------------------------------------------------


def check(profile: dict, **sets) -> list[str]:
    update = ProfileUpdate.model_validate({"profile": profile, "changelog": "c"})
    args = {"review_ids": {1, 2}, "reviewed_appids": {10, 20}, "history_appids": {30}}
    args.update(sets)
    return validate_update(update, **args)


def line(tag: str, **fields) -> dict:
    return {"text": "x", "tag": tag, **fields}


def test_validate_update_accepts_a_sound_profile():
    profile = {
        "loved": [
            line("stated", reviews=[1], games=[10]),
            line("observed", games=[10, 20]),
        ],
        "dealbreakers": [line("stated", reviews=[2])],
        "patterns": [line("observed", games=[20, 10], reviews=[1])],
        "hunches": [line("guess", games=[10, 30], reviews=[1])],
        "open_questions": ["Do you like roguelikes?"],
    }
    assert check(profile) == []
    assert check({}) == []


def test_validate_update_does_not_check_open_questions():
    assert check({"open_questions": ["Is Fake Game (appid 999) any good?"]}) == []


def test_observed_needs_two_different_reviewed_games():
    msg = "Patterns: an observed line must name at least two reviewed games."
    assert check({"patterns": [line("observed", games=[10, 10])]}) == [msg]
    assert check({"patterns": [line("observed")]}) == [msg]
    assert check({"patterns": [line("observed", games=[10, 20])]}) == []
    # A game outside the reviews doesn't count, and is named as its own problem.
    assert check({"patterns": [line("observed", games=[10, 999])]}) == [
        msg,
        "Patterns: a line cites a game that isn't in your reviews.",
    ]
    # Play history is not reviewed.
    assert msg in check({"patterns": [line("observed", games=[10, 30])]})


def test_only_guesses_may_cite_play_history():
    not_reviewed = "{}: a line cites a game that isn't in your reviews."
    assert check({"hunches": [line("guess", games=[30])]}) == []
    assert check({"loved": [line("stated", reviews=[1], games=[30])]}) == [
        not_reviewed.format("What you love")
    ]
    assert not_reviewed.format("Patterns") in check(
        {"patterns": [line("observed", games=[10, 30])]}
    )
    # A guess in another section can cite play history too.
    assert check({"dealbreakers": [line("guess", games=[30])]}) == []
    # Neither list has the game.
    assert check({"hunches": [line("guess", games=[40])]}) == [
        not_reviewed.format("Hunches to test")
    ]


def test_a_stated_line_needs_a_review_it_learned_from():
    needs = "{}: a stated line must cite a review it learned from."
    unknown = "{}: a line cites a review it didn't learn from."
    assert check({"loved": [line("stated")]}) == [needs.format("What you love")]
    # Only reviews it didn't learn from: it has no review to stand on.
    assert check({"dealbreakers": [line("stated", reviews=[77])]}) == [
        needs.format("Dealbreakers"),
        unknown.format("Dealbreakers"),
    ]
    # A real review next to an invented one: the invented one is the problem.
    assert check({"loved": [line("stated", reviews=[1, 77])]}) == [
        unknown.format("What you love")
    ]


def test_any_tag_citing_an_unknown_review_is_refused():
    unknown = "{}: a line cites a review it didn't learn from."
    assert check({"patterns": [line("observed", games=[10, 20], reviews=[77])]}) == [
        unknown.format("Patterns")
    ]
    assert check({"hunches": [line("guess", reviews=[77])]}) == [
        unknown.format("Hunches to test")
    ]


def test_hunches_must_all_be_guesses():
    msg = "Hunches to test: every line must be a guess."
    assert check({"hunches": [line("guess"), line("observed", games=[10, 20])]}) == [
        msg
    ]
    assert check({"hunches": [line("guess"), line("guess")]}) == []


def test_validate_update_lists_every_problem_once_in_section_order():
    profile = {
        "loved": [line("stated"), line("stated"), line("stated", reviews=[77])],
        "dealbreakers": [line("guess", games=[999])],
        "patterns": [line("observed", games=[10])],
        "hunches": [line("stated", reviews=[1])],
    }
    assert check(profile) == [
        "What you love: a stated line must cite a review it learned from.",
        "What you love: a line cites a review it didn't learn from.",
        "Dealbreakers: a line cites a game that isn't in your reviews.",
        "Patterns: an observed line must name at least two reviewed games.",
        "Hunches to test: every line must be a guess.",
    ]


def test_each_line_runs_all_its_checks_in_order():
    profile = {
        "hunches": [line("observed", games=[999], reviews=[77])],
    }
    assert check(profile) == [
        "Hunches to test: every line must be a guess.",
        "Hunches to test: an observed line must name at least two reviewed games.",
        "Hunches to test: a line cites a game that isn't in your reviews.",
        "Hunches to test: a line cites a review it didn't learn from.",
    ]


# --- save_version ---------------------------------------------------------------


def test_save_version_numbers_and_records(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    call_one = fake_call(db, model="model-one", prompt_version="p1")
    call_two = fake_call(db, model="model-two", prompt_version="p2")
    first = save_version(
        db,
        profile,
        ok_update(reviews=[v1.id], games=[10], changelog="First pass."),
        source_ids=[v1.id],
        call=call_one,
        refresh_run_id=None,
        now=FIXED_NOW,
    )
    later = FIXED_NOW + timedelta(hours=1)
    second = save_version(
        db,
        profile,
        ok_update(reviews=[v2.id], games=[20], changelog="Second pass."),
        source_ids=[v2.id],
        call=call_two,
        refresh_run_id=7,
        now=later,
    )
    db.expire_all()
    assert [first.version, second.version] == [1, 2]
    assert first.profile_id == profile.id
    assert first.changelog == "First pass." and second.changelog == "Second pass."
    assert first.content["loved"] == [
        {
            "text": "Likes a cozy co-op night",
            "tag": "stated",
            "games": [10],
            "reviews": [v1.id],
        }
    ]
    assert set(first.content) == {
        "loved",
        "dealbreakers",
        "patterns",
        "hunches",
        "open_questions",
    }
    assert first.content["open_questions"] == [] and first.content["hunches"] == []
    assert (first.ai_call_id, first.refresh_run_id) == (call_one.id, None)
    assert (second.ai_call_id, second.refresh_run_id) == (call_two.id, 7)
    assert (first.model, first.provider_kind) == ("model-one", "anthropic")
    assert (first.prompt_name, first.prompt_version) == ("profile_update", "p1")
    assert second.prompt_version == "p2" and second.model == "model-two"
    assert (first.created_at, second.created_at) == (FIXED_NOW, later)


def test_save_version_links_its_sources(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    saved = learn(db, profile, [v1, v2])
    links = db.execute(
        select(
            ProfileVersionSource.profile_version_id,
            ProfileVersionSource.review_version_id,
        ).order_by(ProfileVersionSource.review_version_id)
    ).all()
    assert links == [(saved.id, v1.id), (saved.id, v2.id)]


def test_save_version_lists_a_repeated_source_once(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    profile = solo_profile(db)
    saved = save_version(
        db,
        profile,
        ok_update(reviews=[v1.id]),
        source_ids=[v1.id, v1.id],
        call=fake_call(db),
        refresh_run_id=None,
        now=FIXED_NOW,
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(ProfileVersionSource)
            .where(ProfileVersionSource.profile_version_id == saved.id)
        )
        == 1
    )


def test_save_version_without_sources_is_refused_and_writes_nothing(db, games):
    profile = solo_profile(db)
    call = fake_call(db)
    with pytest.raises(ValueError):
        save_version(
            db,
            profile,
            ok_update(),
            source_ids=[],
            call=call,
            refresh_run_id=None,
            now=FIXED_NOW,
        )
    assert db.scalar(select(func.count()).select_from(ProfileVersion)) == 0
    assert current_version(db, profile) is None


def test_save_version_commits_once(app, db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    profile = solo_profile(db)
    call = fake_call(db)
    commits = []

    def record(conn):
        commits.append(1)

    event.listen(app.state.engine, "commit", record)
    try:
        save_version(
            db,
            profile,
            ok_update(reviews=[v1.id]),
            source_ids=[v1.id],
            call=call,
            refresh_run_id=None,
            now=FIXED_NOW,
        )
    finally:
        event.remove(app.state.engine, "commit", record)
    assert len(commits) == 1


def test_save_version_failure_leaves_nothing_behind(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    profile = solo_profile(db)
    call = fake_call(db)
    with pytest.raises(IntegrityError):
        save_version(
            db,
            profile,
            ok_update(),
            source_ids=[v1.id, 99999],  # no such review version
            call=call,
            refresh_run_id=None,
            now=FIXED_NOW,
        )
    assert db.scalar(select(func.count()).select_from(ProfileVersion)) == 0
    assert db.scalar(select(func.count()).select_from(ProfileVersionSource)) == 0
    # The session is still usable and the next version is number 1.
    saved = learn(db, profile, [v1])
    assert saved.version == 1


def test_a_saved_version_is_the_profile_content_schema(db, games):
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    profile = solo_profile(db)
    saved = learn(db, profile, [v1])
    db.expire_all()
    content = ProfileContent.model_validate(current_version(db, profile).content)
    assert content.loved[0].reviews == [v1.id] and saved.version == 1


# --- list_profiles --------------------------------------------------------------


def test_list_profiles_summaries(db, games, people):
    sam, alex, crew = people
    other = add_group(db, "Alpha Squad", [sam.id], FIXED_NOW)
    v1 = publish(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    v2 = publish(db, 20, Lane.SOLO, ReviewForm(text="b"), FIXED_NOW)
    profile = solo_profile(db)
    learn(db, profile, [v1])
    second = learn(db, profile, [v2])
    summaries = list_profiles(db)
    assert [s.target.key for s in summaries] == [
        "lane:solo",
        "lane:two",
        "lane:three_four",
        "lane:five_plus",
        f"group:{other.id}",  # "Alpha Squad" before "Sample Crew"
        f"group:{crew.id}",
    ]
    assert [s.target.label for s in summaries] == [
        "Solo",
        "Two players",
        "Three to four",
        "Five or more",
        "Alpha Squad",
        "Sample Crew",
    ]
    solo = summaries[0]
    assert solo.profile.id == profile.id
    assert solo.latest.id == second.id and solo.latest.version == 2
    assert solo.version_count == 2
    assert (solo.target.kind, solo.target.lane, solo.target.group_id) == (
        "lane",
        Lane.SOLO,
        None,
    )
    for empty in summaries[1:]:
        assert (empty.profile, empty.latest, empty.version_count) == (None, None, 0)
    group = summaries[-1]
    assert (group.target.kind, group.target.lane, group.target.group_id) == (
        "group",
        None,
        crew.id,
    )


def test_list_profiles_profile_without_a_version(db, games):
    publish(db, 10, Lane.TWO, ReviewForm(text="a"), FIXED_NOW)
    ensure_profile(db, lane_target(Lane.TWO), FIXED_NOW)
    two = list_profiles(db)[1]
    assert two.profile is not None
    assert (two.latest, two.version_count) == (None, 0)


def test_list_profiles_is_a_fixed_number_of_queries(app, db, games, people):
    sam, alex, crew = people
    _, bare = count_queries(app, lambda: list_profiles(db))
    for number in range(3):
        group = add_group(db, f"Extra {number}", [sam.id], FIXED_NOW)
        publish(
            db,
            10,
            Lane.TWO,
            ReviewForm(text=f"x{number}", named_group_id=group.id),
            FIXED_NOW,
        )
    for lane in Lane:
        version = publish(db, 20, lane, ReviewForm(text="y"), FIXED_NOW)
        profile = ensure_profile(db, lane_target(lane), FIXED_NOW)
        learn(db, profile, [version])
        learn(db, profile, [version])
    summaries, full = count_queries(app, lambda: list_profiles(db))
    assert len(summaries) == 4 + 1 + 3
    assert all(s.version_count == 2 for s in summaries[:4])
    assert full == bare
