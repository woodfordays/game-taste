from datetime import timedelta

import pytest
from conftest import FIXED_NOW
from sqlalchemy import func, select

from app.models import (
    Game,
    Review,
    ReviewDraft,
    ReviewVersion,
    ReviewVersionPerson,
)
from app.services.library import add_store_game
from app.services.people import set_group_members
from app.services.reviews import (
    COMPLEXITIES,
    LANE_LABELS,
    LANE_MAX,
    STATUSES,
    VERDICTS,
    WALK_AWAYS,
    CardSummary,
    Lane,
    LaneState,
    ReviewError,
    ReviewForm,
    ReviewRow,
    card_summary,
    default_lane,
    effective_people,
    head_count_hint,
    history,
    lane_for_head_count,
    lane_states,
    list_reviews,
    load_form,
    publish,
    save_draft,
)
from app.web import lane_label

LATER = FIXED_NOW + timedelta(minutes=1)
LATEST = FIXED_NOW + timedelta(hours=1)

# --- the brief's tests -------------------------------------------------------


def test_draft_never_touches_published(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Good"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=6, text="Rethinking"), FIXED_NOW)
    assert card_summary(db, 10).rating == 8 and card_summary(db, 10).has_draft
    form, source = load_form(db, 10, Lane.SOLO)
    assert (source, form.text) == ("draft", "Rethinking")


def test_publish_versions_and_clears_draft(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="First"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=9, text="Second"), FIXED_NOW)
    v2 = publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=9, text="Second", tags_raw="#Deck Builder"),
        FIXED_NOW,
    )
    assert v2.version == 2 and v2.tags == ["deck_builder"] and v2.steam_hours == 10.0
    assert load_form(db, 10, Lane.SOLO)[1] == "published"
    assert [v.version for v in history(db, 10, Lane.SOLO)] == [2, 1]


def test_publish_rules(db, games):
    with pytest.raises(
        ReviewError, match="Add a rating or a few words before publishing."
    ):
        publish(db, 10, Lane.SOLO, ReviewForm(text="   "), FIXED_NOW)
    with pytest.raises(ReviewError, match="Pick a rating from 1 to 10."):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=11), FIXED_NOW)


def test_text_stored_exactly(db, games):  # Review focus 5
    raw = "  <b>Loved</b> it 🎲\n\nsecond line  "
    assert publish(db, 10, Lane.SOLO, ReviewForm(text=raw), FIXED_NOW).text == raw


def test_lanes_are_separate_and_solo_drops_people(db, games, people):
    sam, alex, crew = people
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=5, people=[sam.id], group_text="x"),
        FIXED_NOW,
    )
    v = publish(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(rating=9, named_group_id=crew.id, group_text="They loved it"),
        FIXED_NOW + timedelta(minutes=1),
    )
    solo = history(db, 10, Lane.SOLO)[0]
    assert solo.group_text == "" and solo.people == []
    assert sorted(p.person_id for p in v.people) == sorted([sam.id, alex.id])
    set_group_members(db, crew.id, [sam.id])
    assert (
        len(history(db, 10, Lane.THREE_FOUR)[0].people) == 2
    )  # past reviews unchanged
    assert card_summary(db, 10) == CardSummary(9, [], Lane.THREE_FOUR, False)
    assert default_lane(db, 10) == Lane.THREE_FOUR and default_lane(db, 20) == Lane.SOLO


def test_head_count_hint():
    assert head_count_hint(Lane.TWO, 1) is None
    assert (
        head_count_hint(Lane.TWO, 2)
        == "You and 2 others make 3 players. This looks like the Three to four lane."
    )
    assert head_count_hint(Lane.FIVE_PLUS, 9) is None


# --- helpers -----------------------------------------------------------------


def count(db, model) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def draft_of(db, appid, lane) -> ReviewDraft | None:
    db.expire_all()
    return db.scalar(
        select(ReviewDraft)
        .join(Review, Review.id == ReviewDraft.review_id)
        .where(Review.appid == appid, Review.lane == lane.value)
    )


def nothing_written(db) -> bool:
    return (
        count(db, Review) == 0
        and count(db, ReviewDraft) == 0
        and count(db, ReviewVersion) == 0
        and count(db, ReviewVersionPerson) == 0
    )


# --- the constants -----------------------------------------------------------


def test_lane_values_labels_and_head_counts():
    assert [lane.value for lane in Lane] == ["solo", "two", "three_four", "five_plus"]
    assert [LANE_LABELS[lane] for lane in Lane] == [
        "Solo",
        "Two players",
        "Three to four",
        "Five or more",
    ]
    assert LANE_MAX == {
        Lane.SOLO: 1,
        Lane.TWO: 2,
        Lane.THREE_FOUR: 4,
        Lane.FIVE_PLUS: None,
    }


def test_the_lane_label_filter_agrees_with_the_labels():
    for lane in Lane:
        assert lane_label(lane) == lane_label(lane.value) == LANE_LABELS[lane]
        assert (
            LANE_LABELS[lane.value] == LANE_LABELS[lane]
        )  # plain strings find them too


def test_allowed_values():
    assert STATUSES == ("finished", "playing", "demo", "dropped")
    assert VERDICTS == ("loved", "liked", "mixed", "disliked")
    assert WALK_AWAYS == ("yes", "partly", "no")
    assert COMPLEXITIES == ("too_light", "about_right", "too_much")


def test_a_form_starts_empty_and_does_not_share_its_people_list():
    a, b = ReviewForm(), ReviewForm()
    assert (a.rating, a.text, a.tags_raw, a.group_text, a.session_length) == (
        None,
        "",
        "",
        "",
        "",
    )
    assert (a.status, a.verdict, a.walk_away, a.complexity) == (None,) * 4
    assert a.people == [] and a.named_group_id is None
    a.people.append(1)
    assert b.people == []


# --- publish: the rules ------------------------------------------------------


@pytest.mark.parametrize("rating", [0, 11, -1, 10**30, True, "8", 7.5])
def test_a_rating_outside_1_to_10_is_refused(db, games, rating):
    with pytest.raises(ReviewError, match="^Pick a rating from 1 to 10.$"):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=rating, text="Words"), FIXED_NOW)
    assert nothing_written(db)


@pytest.mark.parametrize("rating", [1, 5, 10])
def test_every_rating_from_1_to_10_is_accepted(db, games, rating):
    assert (
        publish(db, 10, Lane.SOLO, ReviewForm(rating=rating), FIXED_NOW).rating
        == rating
    )


def test_a_bad_rating_is_reported_before_an_empty_review(db, games):
    with pytest.raises(ReviewError, match="^Pick a rating from 1 to 10.$"):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=11, text=""), FIXED_NOW)


@pytest.mark.parametrize("text", ["", " ", "\t\n  ", " "])
def test_no_rating_and_blank_text_is_refused(db, games, text):
    with pytest.raises(
        ReviewError, match="^Add a rating or a few words before publishing.$"
    ):
        publish(
            db, 10, Lane.SOLO, ReviewForm(text=text, group_text="Only this"), FIXED_NOW
        )
    assert nothing_written(db)


def test_a_rating_alone_publishes(db, games):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=7), FIXED_NOW)
    assert (v.rating, v.text) == (7, "")


def test_words_alone_publish_with_no_rating(db, games):
    v = publish(db, 10, Lane.SOLO, ReviewForm(text=" x "), FIXED_NOW)
    assert (v.rating, v.text) == (None, " x ")


def test_a_refused_publish_leaves_the_draft_and_published_versions_alone(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Kept"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="  "), LATER)
    with pytest.raises(ReviewError):
        publish(db, 10, Lane.SOLO, ReviewForm(text="  "), LATEST)
    assert [v.text for v in history(db, 10, Lane.SOLO)] == ["Kept"]
    assert draft_of(db, 10, Lane.SOLO).text == "  "


def test_an_unknown_game_is_a_lookup_error_and_writes_nothing(db, games):
    with pytest.raises(LookupError):
        publish(db, 999, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    with pytest.raises(LookupError):
        save_draft(db, 999, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    assert nothing_written(db)


def test_an_unknown_game_is_reported_before_a_bad_rating(db, games):
    with pytest.raises(LookupError):
        publish(db, 999, Lane.SOLO, ReviewForm(rating=99), FIXED_NOW)


# --- publish: what is stored -------------------------------------------------


def test_publish_stores_every_field(db, games, people):
    sam, alex, crew = people
    v = publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(
            rating=6,
            text="Main words",
            tags_raw="Co Op, #Co Op, Tense",
            group_text="Group words",
            session_length="about 2 hours",
            status="finished",
            verdict="liked",
            walk_away="partly",
            complexity="about_right",
            people=[alex.id],
            named_group_id=crew.id,
        ),
        FIXED_NOW,
    )
    db.expire_all()
    v = db.get(ReviewVersion, v.id)
    assert (v.rating, v.text, v.group_text) == (6, "Main words", "Group words")
    assert v.tags == ["co_op", "tense"]
    assert (v.status, v.verdict, v.walk_away, v.complexity) == (
        "finished",
        "liked",
        "partly",
        "about_right",
    )
    assert (v.session_length, v.named_group_id) == ("about 2 hours", crew.id)
    assert (v.version, v.steam_hours, v.published_at) == (1, 10.0, FIXED_NOW)
    assert [p.person_id for p in v.people] == sorted([sam.id, alex.id])
    review = db.get(Review, v.review_id)
    assert (review.appid, review.lane, review.created_at) == (10, "two", FIXED_NOW)


def test_the_optional_fields_default_to_none(db, games):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    assert (v.status, v.verdict, v.walk_away, v.complexity, v.named_group_id) == (
        None,
    ) * 5
    assert (v.tags, v.group_text, v.session_length, v.people) == ([], "", "", [])


def test_text_fields_are_stored_exactly_as_given(db, games):  # Review focus 5
    text = "  <b>Loved</b> it 🎲\r\n\r\n  second line\t \n"
    group = "\n  Group <i>words</i> ✨  "
    length = "  2 h  "
    publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(text=text, group_text=group, session_length=length),
        FIXED_NOW,
    )
    db.expire_all()
    v = history(db, 10, Lane.TWO)[0]
    assert (v.text, v.group_text, v.session_length) == (text, group, length)
    # The same through a draft and back out of it.
    save_draft(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(
            text=text, group_text=group, session_length=length, tags_raw=" #A b "
        ),
        FIXED_NOW,
    )
    db.expire_all()
    form, source = load_form(db, 10, Lane.THREE_FOUR)
    assert source == "draft"
    assert (form.text, form.group_text, form.session_length, form.tags_raw) == (
        text,
        group,
        length,
        " #A b ",
    )


def test_steam_hours_come_from_the_playtime_now(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    db.get(Game, 10).playtime_minutes = 645
    db.commit()
    v2 = publish(db, 10, Lane.SOLO, ReviewForm(rating=5), LATER)
    assert [v.steam_hours for v in history(db, 10, Lane.SOLO)] == [10.75, 10.0]
    assert v2.steam_hours == 10.75


def test_steam_hours_are_none_when_the_playtime_is_unknown(db, games):
    add_store_game(db, 30, "Demo Dungeon", FIXED_NOW)
    assert (
        publish(db, 30, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW).steam_hours is None
    )


def test_steam_hours_of_a_game_with_no_playtime_yet_are_zero(db, games):
    db.get(Game, 10).playtime_minutes = 0
    db.commit()
    assert (
        publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW).steam_hours == 0.0
    )


def test_published_at_is_the_time_given(db, games):
    assert publish(db, 10, Lane.SOLO, ReviewForm(rating=5), LATER).published_at == LATER


def test_versions_count_up_per_lane(db, games):
    for _ in range(3):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    assert [v.version for v in history(db, 10, Lane.SOLO)] == [3, 2, 1]
    assert [v.version for v in history(db, 10, Lane.TWO)] == [1]
    assert [v.version for v in history(db, 20, Lane.SOLO)] == [1]
    assert count(db, Review) == 3


def test_earlier_versions_are_kept_as_they_were(db, games):
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=8, text=" One ", tags_raw="a, b", verdict="loved"),
        FIXED_NOW,
    )
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=3, text="Two", tags_raw="c", verdict="disliked"),
        LATER,
    )
    db.expire_all()
    older = history(db, 10, Lane.SOLO)[1]
    assert (
        older.rating,
        older.text,
        older.tags,
        older.verdict,
        older.published_at,
    ) == (
        8,
        " One ",
        ["a", "b"],
        "loved",
        FIXED_NOW,
    )


def test_publish_deletes_only_its_own_lanes_draft(db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="solo wip"), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="two wip"), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="other game wip"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(text="solo wip"), LATER)
    assert draft_of(db, 10, Lane.SOLO) is None
    assert draft_of(db, 10, Lane.TWO).text == "two wip"
    assert draft_of(db, 20, Lane.SOLO).text == "other game wip"


def test_publish_without_a_draft_works(db, games):
    v = publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    assert v.version == 1 and count(db, ReviewDraft) == 0


def test_publish_accepts_a_lane_given_as_its_plain_value(db, games):
    v = publish(db, 10, "three_four", ReviewForm(rating=5), FIXED_NOW)
    assert db.get(Review, v.review_id).lane == "three_four"
    assert [x.version for x in history(db, 10, "three_four")] == [1]


def test_an_unknown_lane_is_a_value_error(db, games):
    with pytest.raises(ValueError):
        publish(db, 10, "squad", ReviewForm(rating=5), FIXED_NOW)
    assert nothing_written(db)


def test_publish_and_save_draft_commit(app, db, games, people):
    sam, _, _ = people
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5, people=[sam.id]), FIXED_NOW)
    # A second session sees both: they are committed, not just flushed.
    with app.state.session_factory() as other:
        assert other.scalar(select(func.count()).select_from(ReviewDraft)) == 1
        assert other.scalar(select(func.count()).select_from(ReviewVersion)) == 1
        assert other.scalar(select(func.count()).select_from(ReviewVersionPerson)) == 1


def test_a_failed_commit_loses_nothing(db, games, monkeypatch):
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Draft words"), FIXED_NOW)

    def fail() -> None:
        raise RuntimeError("disk full")

    real_commit = db.commit
    monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(RuntimeError):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Draft words"), LATER)
    monkeypatch.setattr(db, "commit", real_commit)
    # The version and the draft delete were one transaction, so the draft is still there.
    form, source = load_form(db, 10, Lane.SOLO)
    assert (source, form.text) == ("draft", "Draft words")
    assert history(db, 10, Lane.SOLO) == []


# --- publish: people and the group -------------------------------------------


def test_effective_people_is_the_sorted_union_with_the_group(db, people):
    sam, alex, crew = people
    from app.services.people import add_person

    riley = add_person(db, "Riley", FIXED_NOW)
    assert effective_people(db, [riley.id], None) == [riley.id]
    assert effective_people(db, [riley.id, riley.id], crew.id) == sorted(
        [sam.id, alex.id, riley.id]
    )
    assert effective_people(db, [alex.id], crew.id) == sorted([sam.id, alex.id])
    assert effective_people(db, [], crew.id) == sorted([sam.id, alex.id])
    assert effective_people(db, [], None) == []


def test_effective_people_does_not_touch_its_input(db, people):
    _, alex, crew = people
    given = [alex.id]
    effective_people(db, given, crew.id)
    assert given == [alex.id]


def test_the_group_adds_its_members_to_who_played(db, games, people):
    sam, alex, crew = people
    v = publish(
        db, 10, Lane.TWO, ReviewForm(rating=5, named_group_id=crew.id), FIXED_NOW
    )
    assert [p.person_id for p in v.people] == sorted([sam.id, alex.id])
    assert v.named_group_id == crew.id


def test_people_without_a_group_are_kept_with_no_group(db, games, people):
    sam, _, _ = people
    v = publish(
        db, 10, Lane.TWO, ReviewForm(rating=5, people=[sam.id, sam.id]), FIXED_NOW
    )
    assert [p.person_id for p in v.people] == [sam.id]
    assert v.named_group_id is None


def test_a_group_that_changes_later_does_not_change_old_versions(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.TWO, ReviewForm(rating=5, named_group_id=crew.id), FIXED_NOW)
    set_group_members(db, crew.id, [sam.id])
    publish(db, 10, Lane.TWO, ReviewForm(rating=6, named_group_id=crew.id), LATER)
    new, old = history(db, 10, Lane.TWO)
    assert [p.person_id for p in old.people] == sorted([sam.id, alex.id])
    assert [p.person_id for p in new.people] == [sam.id]


def test_version_people_are_ordered_by_person_id(db, games, people):
    sam, alex, _ = people
    v = publish(
        db, 10, Lane.TWO, ReviewForm(rating=5, people=[alex.id, sam.id]), FIXED_NOW
    )
    db.expire_all()
    ids = [p.person_id for p in db.get(ReviewVersion, v.id).people]
    assert ids == sorted(ids) == sorted([sam.id, alex.id])


def test_solo_clears_people_group_and_group_text_on_publish(db, games, people):
    sam, _, crew = people
    v = publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=5, people=[sam.id], named_group_id=crew.id, group_text=" x "),
        FIXED_NOW,
    )
    assert (v.people, v.named_group_id, v.group_text) == ([], None, "")
    assert count(db, ReviewVersionPerson) == 0


def test_solo_clears_the_same_on_save_draft(db, games, people):
    sam, _, crew = people
    save_draft(
        db,
        10,
        Lane.SOLO,
        ReviewForm(text="wip", people=[sam.id], named_group_id=crew.id, group_text="x"),
        FIXED_NOW,
    )
    form, _ = load_form(db, 10, Lane.SOLO)
    assert (form.people, form.named_group_id, form.group_text, form.text) == (
        [],
        None,
        "",
        "wip",
    )


def test_a_solo_review_ignores_stale_co_op_ids_it_would_drop_anyway(db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip", people=[999]), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5, named_group_id=999), LATER)


@pytest.mark.parametrize("lane", [Lane.TWO, Lane.THREE_FOUR, Lane.FIVE_PLUS])
def test_co_op_lanes_keep_group_text(db, games, lane):
    v = publish(
        db, 10, lane, ReviewForm(rating=5, group_text=" they  liked it "), FIXED_NOW
    )
    assert v.group_text == " they  liked it "


@pytest.mark.parametrize("bad", [[999], [10**30], [-1], ["1"]])
def test_unknown_people_are_refused_on_publish_and_draft(db, games, people, bad):
    sam, _, _ = people
    form = ReviewForm(rating=5, people=[sam.id, *bad])
    with pytest.raises(ReviewError, match="^Pick people from your list.$"):
        publish(db, 10, Lane.TWO, form, FIXED_NOW)
    with pytest.raises(ReviewError, match="^Pick people from your list.$"):
        save_draft(db, 10, Lane.TWO, form, FIXED_NOW)
    assert nothing_written(db)


@pytest.mark.parametrize("bad", [999, 0, 10**30])
def test_an_unknown_group_is_refused_on_publish_and_draft(db, games, bad):
    form = ReviewForm(rating=5, named_group_id=bad)
    with pytest.raises(ReviewError, match="^Pick people from your list.$"):
        publish(db, 10, Lane.TWO, form, FIXED_NOW)
    with pytest.raises(ReviewError, match="^Pick people from your list.$"):
        save_draft(db, 10, Lane.TWO, form, FIXED_NOW)
    assert nothing_written(db)


# --- choice fields -----------------------------------------------------------


CHOICES = [
    ("status", STATUSES),
    ("verdict", VERDICTS),
    ("walk_away", WALK_AWAYS),
    ("complexity", COMPLEXITIES),
]


@pytest.mark.parametrize("field, allowed", CHOICES)
def test_every_listed_option_is_accepted(db, games, field, allowed):
    for value in allowed:
        v = publish(
            db, 10, Lane.SOLO, ReviewForm(rating=5, **{field: value}), FIXED_NOW
        )
        assert getattr(v, field) == value
        save_draft(db, 20, Lane.SOLO, ReviewForm(**{field: value}), FIXED_NOW)
        assert getattr(load_form(db, 20, Lane.SOLO)[0], field) == value


@pytest.mark.parametrize("field, allowed", CHOICES)
@pytest.mark.parametrize("bad", ["", "Loved", "maybe", " liked", 3, ["yes"]])
def test_anything_else_is_refused_on_publish_and_draft(db, games, field, allowed, bad):
    with pytest.raises(ReviewError, match="^Pick one of the listed options.$"):
        publish(db, 10, Lane.SOLO, ReviewForm(rating=5, **{field: bad}), FIXED_NOW)
    with pytest.raises(ReviewError, match="^Pick one of the listed options.$"):
        save_draft(db, 10, Lane.SOLO, ReviewForm(rating=5, **{field: bad}), FIXED_NOW)
    assert nothing_written(db)


# --- save_draft --------------------------------------------------------------


def test_a_draft_is_saved_whatever_state_it_is_in(db, games):
    # Rules 1 and 2 are for publishing: a draft is work in progress.
    save_draft(db, 10, Lane.SOLO, ReviewForm(), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="   "), FIXED_NOW)
    save_draft(db, 10, Lane.THREE_FOUR, ReviewForm(rating=11), FIXED_NOW)
    assert count(db, ReviewDraft) == 3
    assert load_form(db, 10, Lane.THREE_FOUR)[0].rating == 11
    with pytest.raises(ReviewError, match="^Pick a rating from 1 to 10.$"):
        publish(db, 10, Lane.THREE_FOUR, load_form(db, 10, Lane.THREE_FOUR)[0], LATER)


@pytest.mark.parametrize("rating", [10**30, True, "8", 7.5])
def test_a_draft_rating_that_is_not_a_whole_number_is_refused(db, games, rating):
    with pytest.raises(ReviewError, match="^Pick a rating from 1 to 10.$"):
        save_draft(db, 10, Lane.SOLO, ReviewForm(rating=rating), FIXED_NOW)
    assert nothing_written(db)


def test_save_draft_stores_every_field_and_the_time(db, games, people):
    sam, alex, crew = people
    save_draft(
        db,
        10,
        Lane.THREE_FOUR,
        ReviewForm(
            rating=4,
            text="Words",
            tags_raw=" #Raw  tags , here",
            group_text="Group",
            session_length="long",
            status="demo",
            verdict="mixed",
            walk_away="no",
            complexity="too_much",
            people=[alex.id, sam.id, alex.id],
            named_group_id=crew.id,
        ),
        LATER,
    )
    draft = draft_of(db, 10, Lane.THREE_FOUR)
    assert (draft.rating, draft.text, draft.group_text) == (4, "Words", "Group")
    assert draft.tags_raw == " #Raw  tags , here"  # not normalised until publishing
    assert (draft.status, draft.verdict, draft.walk_away, draft.complexity) == (
        "demo",
        "mixed",
        "no",
        "too_much",
    )
    assert (draft.session_length, draft.named_group_id) == ("long", crew.id)
    assert draft.people == [alex.id, sam.id]  # once each, in the order picked
    assert draft.updated_at == LATER
    # The group's members are not copied into the draft: the group is still the source.
    form, source = load_form(db, 10, Lane.THREE_FOUR)
    assert source == "draft" and form.named_group_id == crew.id


def test_save_draft_returns_nothing_and_creates_the_review_once(db, games):
    assert save_draft(db, 10, Lane.SOLO, ReviewForm(text="one"), FIXED_NOW) is None
    review = db.scalar(select(Review))
    assert (review.appid, review.lane, review.created_at) == (10, "solo", FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="two"), LATER)
    assert count(db, Review) == 1 and count(db, ReviewDraft) == 1


def test_saving_again_replaces_the_one_draft(db, games, people):
    sam, _, _ = people
    save_draft(
        db,
        10,
        Lane.TWO,
        ReviewForm(rating=3, text="first", verdict="mixed", people=[sam.id]),
        FIXED_NOW,
    )
    save_draft(db, 10, Lane.TWO, ReviewForm(text="second"), LATER)
    draft = draft_of(db, 10, Lane.TWO)
    assert (draft.text, draft.rating, draft.verdict, draft.people) == (
        "second",
        None,
        None,
        [],
    )
    assert draft.updated_at == LATER
    assert count(db, ReviewDraft) == 1


def test_a_draft_after_publishing_leaves_the_review_and_versions_alone(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Good"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(rating=1, text="Hmm"), LATER)
    assert count(db, Review) == 1 and count(db, ReviewVersion) == 1
    [v] = history(db, 10, Lane.SOLO)
    assert (v.rating, v.text, v.published_at) == (8, "Good", FIXED_NOW)


def test_drafts_in_other_lanes_and_games_are_independent(db, games):
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="a"), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="b"), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="c"), FIXED_NOW)
    assert [
        load_form(db, a, l)[0].text
        for a, l in [(10, Lane.SOLO), (10, Lane.TWO), (20, Lane.SOLO)]
    ] == [
        "a",
        "b",
        "c",
    ]


def test_a_draft_does_not_mutate_the_form_it_was_given(db, games, people):
    sam, _, crew = people
    form = ReviewForm(
        text="wip", people=[sam.id], named_group_id=crew.id, group_text="g"
    )
    save_draft(db, 10, Lane.SOLO, form, FIXED_NOW)
    publish(db, 10, Lane.SOLO, form, LATER)
    assert (form.people, form.named_group_id, form.group_text) == (
        [sam.id],
        crew.id,
        "g",
    )


# --- load_form ---------------------------------------------------------------


def test_a_game_never_reviewed_loads_an_empty_form(db, games):
    form, source = load_form(db, 10, Lane.SOLO)
    assert source == "new" and form == ReviewForm()
    assert count(db, Review) == 0  # looking creates nothing


def test_an_unknown_game_loads_an_empty_form_too(db):
    assert load_form(db, 999, Lane.SOLO) == (ReviewForm(), "new")


def test_a_published_review_loads_its_latest_version(db, games, people):
    sam, alex, crew = people
    publish(db, 10, Lane.TWO, ReviewForm(rating=3, text="old"), FIXED_NOW)
    publish(
        db,
        10,
        Lane.TWO,
        ReviewForm(
            rating=9,
            text=" new ",
            tags_raw="Deck Builder, #Co Op",
            group_text="Group",
            session_length="2 h",
            status="finished",
            verdict="loved",
            walk_away="yes",
            complexity="too_light",
            named_group_id=crew.id,
        ),
        LATER,
    )
    form, source = load_form(db, 10, Lane.TWO)
    assert source == "published"
    assert form == ReviewForm(
        rating=9,
        text=" new ",
        tags_raw="deck_builder, co_op",
        group_text="Group",
        session_length="2 h",
        status="finished",
        verdict="loved",
        walk_away="yes",
        complexity="too_light",
        people=sorted([sam.id, alex.id]),
        named_group_id=crew.id,
    )


def test_the_draft_wins_over_the_published_version(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Published"), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="Draft"), LATER)
    assert load_form(db, 10, Lane.SOLO)[0].text == "Draft"
    publish(db, 10, Lane.SOLO, ReviewForm(rating=7, text="Draft"), LATEST)
    form, source = load_form(db, 10, Lane.SOLO)
    assert (source, form.rating) == ("published", 7)


def test_load_form_only_reads_its_own_lane(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, text="Solo"), FIXED_NOW)
    assert load_form(db, 10, Lane.TWO) == (ReviewForm(), "new")


def test_forms_loaded_are_independent_copies(db, games, people):
    sam, _, _ = people
    publish(
        db, 10, Lane.TWO, ReviewForm(rating=5, tags_raw="a", people=[sam.id]), FIXED_NOW
    )
    form, _ = load_form(db, 10, Lane.TWO)
    form.people.append(999)
    form.tags_raw = "changed"
    assert load_form(db, 10, Lane.TWO)[0].people == [sam.id]
    assert history(db, 10, Lane.TWO)[0].tags == ["a"]
    assert [p.person_id for p in history(db, 10, Lane.TWO)[0].people] == [sam.id]


def test_a_loaded_form_publishes_again_as_the_next_version(db, games):
    publish(
        db,
        10,
        Lane.SOLO,
        ReviewForm(rating=8, text=" a ", tags_raw="One, Two"),
        FIXED_NOW,
    )
    form, _ = load_form(db, 10, Lane.SOLO)
    v2 = publish(db, 10, Lane.SOLO, form, LATER)
    assert (v2.version, v2.rating, v2.text, v2.tags) == (2, 8, " a ", ["one", "two"])


# --- head_count_hint ---------------------------------------------------------


@pytest.mark.parametrize(
    "lane, others, label",
    [
        (Lane.TWO, 2, "Three to four"),
        (Lane.TWO, 3, "Three to four"),
        (Lane.TWO, 4, "Five or more"),
        (Lane.THREE_FOUR, 4, "Five or more"),
        (Lane.THREE_FOUR, 10, "Five or more"),
        (Lane.SOLO, 2, "Three to four"),
    ],
)
def test_the_hint_names_the_lane_the_head_count_falls_in(lane, others, label):
    assert head_count_hint(lane, others) == (
        f"You and {others} others make {others + 1} players. This looks like the {label} lane."
    )


def test_solo_with_one_other_reads_as_two_players():
    assert head_count_hint(Lane.SOLO, 1) == (
        "You and 1 others make 2 players. This looks like the Two players lane."
    )


@pytest.mark.parametrize(
    "lane, others",
    [
        (Lane.SOLO, 0),
        (Lane.TWO, 0),
        (Lane.TWO, 1),
        (Lane.THREE_FOUR, 0),
        (Lane.THREE_FOUR, 2),
        (Lane.THREE_FOUR, 3),
        (Lane.FIVE_PLUS, 0),
        (Lane.FIVE_PLUS, 20),
    ],
)
def test_no_hint_when_the_lane_has_room(lane, others):
    assert head_count_hint(lane, others) is None


@pytest.mark.parametrize(
    "heads, lane",
    [
        (1, Lane.SOLO),
        (2, Lane.TWO),
        (3, Lane.THREE_FOUR),
        (4, Lane.THREE_FOUR),
        (5, Lane.FIVE_PLUS),
        (12, Lane.FIVE_PLUS),
    ],
)
def test_lane_for_head_count(heads, lane):
    assert lane_for_head_count(heads) == lane


# --- lane_states -------------------------------------------------------------


def test_lane_states_cover_all_four_lanes(db, games):
    assert lane_states(db, 10) == {lane: LaneState(False, False) for lane in Lane}
    assert list(lane_states(db, 10)) == list(Lane)
    assert list(lane_states(db, 999)) == list(Lane)


def test_lane_states_tell_drafts_from_published(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="wip"), FIXED_NOW)
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.THREE_FOUR, ReviewForm(text="wip"), LATER)
    publish(db, 20, Lane.FIVE_PLUS, ReviewForm(rating=5), FIXED_NOW)
    assert lane_states(db, 10) == {
        Lane.SOLO: LaneState(True, False),
        Lane.TWO: LaneState(False, True),
        Lane.THREE_FOUR: LaneState(True, True),
        Lane.FIVE_PLUS: LaneState(False, False),
    }
    assert lane_states(db, 20)[Lane.FIVE_PLUS] == LaneState(True, False)


# --- default_lane ------------------------------------------------------------


def test_default_lane_is_solo_with_no_reviews(db, games):
    assert default_lane(db, 10) == Lane.SOLO
    assert default_lane(db, 999) == Lane.SOLO


def test_default_lane_follows_the_most_recent_publish(db, games):
    publish(db, 10, Lane.THREE_FOUR, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), LATER)
    assert default_lane(db, 10) == Lane.TWO


def test_default_lane_follows_a_more_recent_draft(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.FIVE_PLUS, ReviewForm(text="wip"), LATER)
    assert default_lane(db, 10) == Lane.FIVE_PLUS


def test_default_lane_counts_the_newer_of_a_lanes_draft_and_published(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), LATEST)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="old wip"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), LATER)
    assert default_lane(db, 10) == Lane.SOLO


def first_listed_lane(db, appid) -> Lane:
    """The lane of the game's first row in `list_reviews`."""
    return next(r.lane for r in list_reviews(db, None, None) if r.game.appid == appid)


def test_default_lane_ties_between_published_lanes_go_to_the_higher_version_id(
    db, games
):
    # Same timestamp, so only the ids say which lane was published last. Both orders,
    # so it can't be the lane order (or the row the database returns first) deciding.
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    assert default_lane(db, 10) == Lane.SOLO
    publish(db, 20, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 20, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    assert default_lane(db, 20) == Lane.TWO


def test_default_lane_ties_between_drafts_go_to_the_later_lane(db, games):
    for lane in (Lane.THREE_FOUR, Lane.SOLO, Lane.TWO):
        save_draft(db, 10, lane, ReviewForm(text="wip"), FIXED_NOW)
    assert default_lane(db, 10) == Lane.THREE_FOUR


def test_default_lane_a_published_version_beats_a_draft_at_the_same_time(db, games):
    # The draft is in the later lane, so the lane order would pick it if it were used.
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.THREE_FOUR, ReviewForm(text="wip"), FIXED_NOW)
    assert default_lane(db, 10) == Lane.TWO
    save_draft(db, 20, Lane.THREE_FOUR, ReviewForm(text="wip"), FIXED_NOW)
    publish(db, 20, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    assert default_lane(db, 20) == Lane.TWO


def test_default_lane_a_drafts_time_carries_no_version_id(db, games):
    # Solo's latest version has the higher id, but Solo's time comes from its draft,
    # so that id doesn't count. Two was published at the same time, so Two wins.
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), LATER)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), LATER)
    assert default_lane(db, 10) == Lane.TWO


def test_default_lane_looks_only_at_that_game(db, games):
    publish(db, 20, Lane.FIVE_PLUS, ReviewForm(rating=5), LATEST)
    assert default_lane(db, 10) == Lane.SOLO


# --- card_summary ------------------------------------------------------------


def test_card_summary_with_nothing(db, games):
    assert card_summary(db, 10) == CardSummary(None, [], None, False)
    assert card_summary(db, 999) == CardSummary(None, [], None, False)


def test_card_summary_with_only_a_draft(db, games):
    save_draft(db, 10, Lane.TWO, ReviewForm(rating=9, text="wip"), FIXED_NOW)
    assert card_summary(db, 10) == CardSummary(None, [], None, True)


def test_card_summary_uses_the_latest_version_across_lanes(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=9, tags_raw="solo tag"), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=4, tags_raw="Two Tag, b"), LATER)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=7, tags_raw="newest"), LATEST)
    assert card_summary(db, 10) == CardSummary(7, ["newest"], Lane.SOLO, False)


def test_card_summary_of_a_version_without_a_rating(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=9), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(text="Words only"), LATER)
    assert card_summary(db, 10).rating is None


def test_card_summary_ties_go_to_the_higher_version_id(db, games):
    # Same timestamp, so only the id says which was published last. Both orders, so
    # the answer can't just be whatever row the database happens to return first.
    publish(db, 10, Lane.TWO, ReviewForm(rating=1), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=2), FIXED_NOW)
    assert card_summary(db, 10) == CardSummary(2, [], Lane.SOLO, False)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=3), FIXED_NOW)
    publish(db, 20, Lane.TWO, ReviewForm(rating=4), FIXED_NOW)
    assert card_summary(db, 20) == CardSummary(4, [], Lane.TWO, False)


def test_card_summary_has_draft_if_any_lane_has_one(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.FIVE_PLUS, ReviewForm(text="wip"), LATER)
    assert card_summary(db, 10).has_draft is True
    assert card_summary(db, 20).has_draft is False


def test_card_summary_tags_are_a_copy(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5, tags_raw="a"), FIXED_NOW)
    card_summary(db, 10).tags.append("b")
    assert card_summary(db, 10).tags == ["a"]


# --- history -----------------------------------------------------------------


def test_history_is_newest_first_and_per_lane(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(text="one"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(text="two"), LATER)
    publish(db, 10, Lane.TWO, ReviewForm(text="other lane"), LATER)
    publish(db, 20, Lane.SOLO, ReviewForm(text="other game"), LATER)
    assert [v.text for v in history(db, 10, Lane.SOLO)] == ["two", "one"]


def test_history_of_a_lane_without_versions_is_empty(db, games):
    assert history(db, 10, Lane.SOLO) == []
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), FIXED_NOW)
    assert history(db, 10, Lane.SOLO) == []
    assert history(db, 999, Lane.SOLO) == []


# --- list_reviews ------------------------------------------------------------


def test_list_reviews_has_a_row_per_game_and_lane(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=9, verdict="loved"), LATER)
    publish(db, 10, Lane.TWO, ReviewForm(rating=4, verdict="mixed"), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="wip"), LATEST)
    rows = list_reviews(db, None, None)
    assert [(r.game.name, r.lane) for r in rows] == [
        ("Test Tactics", Lane.SOLO),
        ("Sample Quest", Lane.SOLO),
        ("Sample Quest", Lane.TWO),
    ]
    test_tactics, solo, two = rows
    assert all(isinstance(r, ReviewRow) for r in rows)
    assert (test_tactics.latest, test_tactics.has_draft) == (None, True)
    assert (solo.latest.version, solo.latest.rating, solo.has_draft) == (2, 9, False)
    assert (two.latest.rating, two.has_draft) == (4, False)


def test_list_reviews_updated_at_is_the_newer_of_draft_and_published(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.TWO, ReviewForm(text="wip"), LATER)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="wip"), LATEST)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)  # clears that draft
    by_key = {(r.game.appid, r.lane): r for r in list_reviews(db, None, None)}
    assert by_key[(10, Lane.SOLO)].updated_at == FIXED_NOW
    assert by_key[(10, Lane.TWO)].updated_at == LATER
    assert by_key[(10, Lane.TWO)].has_draft is True
    assert by_key[(20, Lane.SOLO)].updated_at == FIXED_NOW


def test_list_reviews_is_newest_first(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 20, Lane.SOLO, ReviewForm(text="wip"), LATEST)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), LATER)
    assert [(r.game.appid, r.lane) for r in list_reviews(db, None, None)] == [
        (20, Lane.SOLO),
        (10, Lane.TWO),
        (10, Lane.SOLO),
    ]


def test_list_reviews_ties_go_to_the_higher_version_id_then_the_later_lane(db, games):
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.FIVE_PLUS, ReviewForm(text="a"), FIXED_NOW)
    save_draft(db, 10, Lane.THREE_FOUR, ReviewForm(text="b"), FIXED_NOW)
    save_draft(db, 20, Lane.THREE_FOUR, ReviewForm(text="c"), FIXED_NOW)
    listed = [(r.game.appid, r.lane) for r in list_reviews(db, None, None)]
    assert listed == [
        (10, Lane.SOLO),  # the version published last
        (10, Lane.TWO),
        (10, Lane.FIVE_PLUS),  # then drafts: later lane first, then higher app id
        (20, Lane.THREE_FOUR),
        (10, Lane.THREE_FOUR),
    ]


def test_list_reviews_a_published_version_beats_a_draft_at_the_same_time(db, games):
    # A draft has no version id, whichever of the two was saved first.
    save_draft(db, 20, Lane.FIVE_PLUS, ReviewForm(text="wip"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.THREE_FOUR, ReviewForm(text="wip"), FIXED_NOW)
    assert [(r.game.appid, r.lane) for r in list_reviews(db, None, None)] == [
        (10, Lane.SOLO),
        (20, Lane.FIVE_PLUS),
        (10, Lane.THREE_FOUR),
    ]


def test_list_reviews_a_drafts_time_carries_no_version_id(db, games):
    # Same setup as the default_lane test of that name: Solo's time comes from its
    # draft, so its latest version's higher id doesn't put it ahead of Two.
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), LATER)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 10, Lane.SOLO, ReviewForm(text="wip"), LATER)
    assert [r.lane for r in list_reviews(db, None, None)] == [Lane.TWO, Lane.SOLO]


# --- one rule for "most recent" ----------------------------------------------


@pytest.mark.parametrize(
    "first, second",
    [
        (Lane.TWO, Lane.SOLO),
        (Lane.SOLO, Lane.TWO),
        (Lane.THREE_FOUR, Lane.FIVE_PLUS),
        (Lane.FIVE_PLUS, Lane.THREE_FOUR),
    ],
)
def test_the_card_the_default_lane_and_the_list_agree_on_a_published_tie(
    db, games, first, second
):
    # Two lanes published at the same `now`: the one published second is the most
    # recent, whatever its place in the lane order.
    publish(db, 10, first, ReviewForm(rating=1), FIXED_NOW)
    publish(db, 10, second, ReviewForm(rating=2), FIXED_NOW)
    assert card_summary(db, 10).lane == second
    assert default_lane(db, 10) == second
    assert first_listed_lane(db, 10) == second


@pytest.mark.parametrize("published_first", [True, False])
def test_the_default_lane_and_the_list_agree_on_a_draft_and_a_published_tie(
    db, games, published_first
):
    # The published lane is the earlier one in the lane order, the draft the later.
    if published_first:
        publish(db, 10, Lane.TWO, ReviewForm(rating=1), FIXED_NOW)
    save_draft(db, 10, Lane.FIVE_PLUS, ReviewForm(text="wip"), FIXED_NOW)
    if not published_first:
        publish(db, 10, Lane.TWO, ReviewForm(rating=1), FIXED_NOW)
    assert default_lane(db, 10) == Lane.TWO
    assert first_listed_lane(db, 10) == Lane.TWO
    assert card_summary(db, 10) == CardSummary(1, [], Lane.TWO, True)


def test_the_default_lane_and_the_list_agree_on_a_tie_between_drafts(db, games):
    for lane in (Lane.TWO, Lane.FIVE_PLUS, Lane.SOLO):
        save_draft(db, 10, lane, ReviewForm(text="wip"), FIXED_NOW)
    assert default_lane(db, 10) == Lane.FIVE_PLUS
    assert first_listed_lane(db, 10) == Lane.FIVE_PLUS


def test_list_reviews_filters_by_lane(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=5), FIXED_NOW)
    publish(db, 10, Lane.TWO, ReviewForm(rating=5), FIXED_NOW)
    save_draft(db, 20, Lane.TWO, ReviewForm(text="wip"), FIXED_NOW)
    assert [(r.game.appid, r.lane) for r in list_reviews(db, Lane.SOLO, None)] == [
        (10, Lane.SOLO)
    ]
    assert {r.game.appid for r in list_reviews(db, Lane.TWO, None)} == {10, 20}
    assert list_reviews(db, Lane.FIVE_PLUS, None) == []


def test_list_reviews_filters_on_the_latest_versions_verdict(db, games):
    publish(db, 10, Lane.SOLO, ReviewForm(rating=8, verdict="liked"), FIXED_NOW)
    publish(db, 10, Lane.SOLO, ReviewForm(rating=3, verdict="disliked"), LATER)
    publish(db, 10, Lane.TWO, ReviewForm(rating=8, verdict="liked"), FIXED_NOW)
    publish(db, 20, Lane.SOLO, ReviewForm(rating=8), FIXED_NOW)  # no verdict
    save_draft(db, 20, Lane.TWO, ReviewForm(text="wip", verdict="liked"), FIXED_NOW)
    liked = list_reviews(db, None, "liked")
    assert [(r.game.appid, r.lane) for r in liked] == [(10, Lane.TWO)]
    assert [(r.game.appid, r.lane) for r in list_reviews(db, None, "disliked")] == [
        (10, Lane.SOLO)
    ]
    assert list_reviews(db, None, "loved") == []
    assert [r.lane for r in list_reviews(db, Lane.TWO, "liked")] == [Lane.TWO]
    assert list_reviews(db, Lane.SOLO, "liked") == []


def test_list_reviews_with_nothing(db, games):
    assert list_reviews(db, None, None) == []


# --- the relationship --------------------------------------------------------


def test_version_people_are_a_relationship_to_the_person_rows(db, games, people):
    sam, alex, _ = people
    v = publish(
        db, 10, Lane.TWO, ReviewForm(rating=5, people=[sam.id, alex.id]), FIXED_NOW
    )
    assert all(isinstance(p, ReviewVersionPerson) for p in v.people)
    assert {(p.version_id, p.person_id) for p in v.people} == {
        (v.id, sam.id),
        (v.id, alex.id),
    }
    assert count(db, ReviewVersionPerson) == 2
