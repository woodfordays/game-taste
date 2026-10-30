import html
import re

import pytest
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth import advance_step
from app.models import GroupMember, NamedGroup, Person
from app.services import people
from app.services.people import (
    PeopleError,
    add_group,
    add_person,
    group_member_ids,
    list_groups,
    list_people,
    parse_member_ids,
    rename_group,
    rename_person,
    set_group_members,
)
from app.services.settings import get_setting
from conftest import FIXED_NOW

# --- the brief's tests -------------------------------------------------------


def test_people_rules(db):
    sam = add_person(db, "  Sam ", FIXED_NOW)
    assert sam.name == "Sam"
    with pytest.raises(PeopleError, match="You already have someone named Sam."):
        add_person(db, "sam", FIXED_NOW)
    with pytest.raises(PeopleError, match="Enter a name."):
        add_person(db, "   ", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    group = add_group(db, "Sample Crew", [sam.id, alex.id], FIXED_NOW)
    assert sorted(group_member_ids(db, group.id)) == sorted([sam.id, alex.id])


def test_no_built_in_names(client_at_people_step, db):
    assert list_people(db) == [] and list_groups(db) == []


# --- helpers -----------------------------------------------------------------


def names(db) -> list[str]:
    return [person.name for person in list_people(db)]


def counts(db) -> tuple[int, int, int]:
    """Rows in person, named_group and group_member, to show a failure saved nothing."""
    db.expire_all()
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (Person, NamedGroup, GroupMember)
    )


# --- add_person --------------------------------------------------------------


def test_add_person_saves_the_stripped_name_and_the_time(db):
    sam = add_person(db, "\t Sam \n", FIXED_NOW)
    assert sam.id is not None and sam.name == "Sam" and sam.created_at == FIXED_NOW
    assert names(db) == ["Sam"]


def test_inner_whitespace_is_kept_as_typed(db):
    assert add_person(db, "Sam  B", FIXED_NOW).name == "Sam  B"


@pytest.mark.parametrize("blank", ["", " ", "\t\n  "])
def test_a_blank_name_is_refused(db, blank):
    with pytest.raises(PeopleError, match="^Enter a name.$"):
        add_person(db, blank, FIXED_NOW)
    assert names(db) == []


def test_a_name_may_be_sixty_characters_but_not_more(db):
    longest = "S" * 60
    assert add_person(db, f"  {longest}  ", FIXED_NOW).name == longest
    with pytest.raises(PeopleError, match="^Use 60 characters or fewer.$"):
        add_person(db, "A" * 61, FIXED_NOW)
    assert names(db) == [longest]


def test_a_blank_name_is_reported_before_a_long_one(db):
    with pytest.raises(PeopleError, match="^Enter a name.$"):
        add_person(db, "   ", FIXED_NOW)


@pytest.mark.parametrize("again", ["sam", "SAM", " Sam ", "sAm"])
def test_a_duplicate_is_refused_whatever_the_case(db, again):
    add_person(db, "Sam", FIXED_NOW)
    # The message names the person already saved, spelled the way it was saved.
    with pytest.raises(PeopleError, match="^You already have someone named Sam.$"):
        add_person(db, again, FIXED_NOW)
    assert names(db) == ["Sam"]


def test_a_duplicate_beyond_ascii_is_refused_too(db):
    add_person(db, "Émile", FIXED_NOW)
    with pytest.raises(PeopleError, match="^You already have someone named Émile.$"):
        add_person(db, "émile", FIXED_NOW)


def test_messages_are_plain_strings(db):
    # They can quote what the user typed, so they must never be markup.
    add_person(db, "<b>Sam</b>", FIXED_NOW)
    with pytest.raises(PeopleError) as caught:
        add_person(db, "<B>sam</B>", FIXED_NOW)
    message = str(caught.value)
    assert message == "You already have someone named <b>Sam</b>."
    assert not isinstance(message, Markup)


def test_a_person_and_a_group_may_share_a_name(db):
    add_person(db, "Sam", FIXED_NOW)
    assert add_group(db, "Sam", [], FIXED_NOW).name == "Sam"


# --- rename_person -----------------------------------------------------------


def test_rename_person(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    renamed = rename_person(db, sam.id, "  Samuel ")
    assert renamed.id == sam.id and renamed.name == "Samuel"
    assert names(db) == ["Samuel"]


def test_rename_person_may_change_only_the_case(db):
    sam = add_person(db, "sam", FIXED_NOW)
    assert rename_person(db, sam.id, "Sam").name == "Sam"
    assert rename_person(db, sam.id, "Sam").name == "Sam"


def test_rename_person_follows_the_same_rules(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    add_person(db, "Alex", FIXED_NOW)
    with pytest.raises(PeopleError, match="^Enter a name.$"):
        rename_person(db, sam.id, "  ")
    with pytest.raises(PeopleError, match="^Use 60 characters or fewer.$"):
        rename_person(db, sam.id, "S" * 61)
    with pytest.raises(PeopleError, match="^You already have someone named Alex.$"):
        rename_person(db, sam.id, " ALEX ")
    assert names(db) == ["Alex", "Sam"]


def test_rename_an_unknown_person(db):
    with pytest.raises(PeopleError, match="Couldn't find that person."):
        rename_person(db, 999, "Sam")


# --- add_group ---------------------------------------------------------------


def test_add_group_saves_the_name_the_time_and_the_members(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    group = add_group(db, "  Sample Crew ", [alex.id, sam.id], FIXED_NOW)
    assert group.name == "Sample Crew" and group.created_at == FIXED_NOW
    assert sorted(group_member_ids(db, group.id)) == sorted([sam.id, alex.id])


def test_add_group_ignores_a_repeated_member(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    group = add_group(db, "Sample Crew", [sam.id, sam.id, sam.id], FIXED_NOW)
    assert group_member_ids(db, group.id) == [sam.id]


def test_add_group_without_members(db):
    group = add_group(db, "Sample Crew", [], FIXED_NOW)
    assert group_member_ids(db, group.id) == []
    assert counts(db) == (0, 1, 0)


@pytest.mark.parametrize("again", ["sample crew", "SAMPLE CREW", "  Sample Crew "])
def test_a_duplicate_group_is_refused_whatever_the_case(db, again):
    add_group(db, "Sample Crew", [], FIXED_NOW)
    with pytest.raises(
        PeopleError, match="^You already have a group named Sample Crew.$"
    ):
        add_group(db, again, [], FIXED_NOW)
    assert counts(db) == (0, 1, 0)


def test_group_name_rules(db):
    with pytest.raises(PeopleError, match="^Enter a name.$"):
        add_group(db, "  ", [], FIXED_NOW)
    with pytest.raises(PeopleError, match="^Use 60 characters or fewer.$"):
        add_group(db, "G" * 61, [], FIXED_NOW)
    assert add_group(db, "G" * 60, [], FIXED_NOW).name == "G" * 60


def test_an_unknown_member_refuses_the_whole_group(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    with pytest.raises(PeopleError, match="^Pick people from your list.$"):
        add_group(db, "Sample Crew", [sam.id, 999], FIXED_NOW)
    # Not even the group itself was saved.
    assert counts(db) == (1, 0, 0)


def test_a_member_id_too_large_for_the_database_is_just_unknown(db):
    with pytest.raises(PeopleError, match="^Pick people from your list.$"):
        add_group(db, "Sample Crew", [10**30], FIXED_NOW)
    assert counts(db) == (0, 0, 0)


# --- rename_group ------------------------------------------------------------


def test_rename_group(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    group = add_group(db, "Sample Crew", [sam.id], FIXED_NOW)
    renamed = rename_group(db, group.id, "  Test Crew ")
    assert renamed.id == group.id and renamed.name == "Test Crew"
    assert group_member_ids(db, group.id) == [sam.id]


def test_rename_group_may_change_only_the_case(db):
    group = add_group(db, "sample crew", [], FIXED_NOW)
    assert rename_group(db, group.id, "Sample Crew").name == "Sample Crew"


def test_rename_group_follows_the_same_rules(db):
    group = add_group(db, "Sample Crew", [], FIXED_NOW)
    add_group(db, "Test Crew", [], FIXED_NOW)
    with pytest.raises(PeopleError, match="^Enter a name.$"):
        rename_group(db, group.id, "")
    with pytest.raises(PeopleError, match="^Use 60 characters or fewer.$"):
        rename_group(db, group.id, "G" * 61)
    with pytest.raises(
        PeopleError, match="^You already have a group named Test Crew.$"
    ):
        rename_group(db, group.id, "test crew")
    assert [g.name for g, _ in list_groups(db)] == ["Sample Crew", "Test Crew"]


def test_rename_an_unknown_group(db):
    with pytest.raises(PeopleError, match="Couldn't find that group."):
        rename_group(db, 999, "Sample Crew")


# --- set_group_members -------------------------------------------------------


def test_set_group_members_replaces_the_members(db):
    sam, alex, riley = (add_person(db, n, FIXED_NOW) for n in ("Sam", "Alex", "Riley"))
    group = add_group(db, "Sample Crew", [sam.id, alex.id], FIXED_NOW)
    set_group_members(db, group.id, [alex.id, riley.id, riley.id])
    assert sorted(group_member_ids(db, group.id)) == sorted([alex.id, riley.id])
    set_group_members(db, group.id, [])
    assert group_member_ids(db, group.id) == []
    assert names(db) == ["Alex", "Riley", "Sam"]  # the people themselves stay


def test_set_group_members_leaves_other_groups_alone(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    one = add_group(db, "Sample Crew", [sam.id], FIXED_NOW)
    two = add_group(db, "Test Crew", [sam.id], FIXED_NOW)
    set_group_members(db, one.id, [])
    assert group_member_ids(db, two.id) == [sam.id]


def test_set_group_members_with_an_unknown_person_changes_nothing(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    group = add_group(db, "Sample Crew", [sam.id], FIXED_NOW)
    with pytest.raises(PeopleError, match="^Pick people from your list.$"):
        set_group_members(db, group.id, [999])
    assert group_member_ids(db, group.id) == [sam.id]


def test_set_group_members_of_an_unknown_group(db):
    with pytest.raises(PeopleError, match="Couldn't find that group."):
        set_group_members(db, 999, [])


# --- queries -----------------------------------------------------------------


def test_list_people_is_by_name_ignoring_case(db):
    for name in ("sam", "Riley", "alex"):
        add_person(db, name, FIXED_NOW)
    assert names(db) == ["alex", "Riley", "sam"]


def test_list_groups_pairs_each_group_with_its_members_by_name(db):
    sam = add_person(db, "Sam", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    add_person(db, "Riley", FIXED_NOW)
    add_group(db, "Test Crew", [], FIXED_NOW)
    add_group(db, "Sample Crew", [sam.id, alex.id], FIXED_NOW)
    listed = list_groups(db)
    assert [(g.name, [p.name for p in members]) for g, members in listed] == [
        ("Sample Crew", ["Alex", "Sam"]),
        ("Test Crew", []),
    ]


def test_group_member_ids_of_an_unknown_group_is_empty(db):
    assert group_member_ids(db, 999) == []


# --- the unique index is the last guard --------------------------------------


@pytest.fixture
def blind_once(monkeypatch):
    """Make the duplicate check miss once, as if another request saved the same name
    between the check and the save. The second lookup, made to build the message, is real."""
    real = people._find_by_name
    calls = []

    def lookup(*args, **kwargs):
        calls.append(args)
        return None if len(calls) == 1 else real(*args, **kwargs)

    monkeypatch.setattr(people, "_find_by_name", lookup)


def test_a_person_the_check_missed_is_caught_by_the_index(db, blind_once):
    add_person(db, "Sam", FIXED_NOW)
    with pytest.raises(PeopleError, match="^You already have someone named Sam.$"):
        add_person(db, "sam", FIXED_NOW)
    # The session was rolled back and is still usable.
    assert names(db) == ["Sam"]
    assert add_person(db, "Alex", FIXED_NOW).name == "Alex"


def test_a_group_the_check_missed_is_caught_by_the_index(db, blind_once):
    sam = add_person(db, "Sam", FIXED_NOW)
    add_group(db, "Sample Crew", [sam.id], FIXED_NOW)
    with pytest.raises(
        PeopleError, match="^You already have a group named Sample Crew.$"
    ):
        add_group(db, "sample crew", [sam.id], FIXED_NOW)
    assert counts(db) == (1, 1, 1)


def test_a_rename_the_check_missed_is_caught_by_the_index(db, blind_once):
    add_person(db, "Sam", FIXED_NOW)
    alex = add_person(db, "Alex", FIXED_NOW)
    with pytest.raises(PeopleError, match="^You already have someone named Sam.$"):
        rename_person(db, alex.id, "sam")
    assert names(db) == ["Alex", "Sam"]


def test_a_group_rename_the_check_missed_is_caught_by_the_index(db, blind_once):
    add_group(db, "Sample Crew", [], FIXED_NOW)
    other = add_group(db, "Test Crew", [], FIXED_NOW)
    with pytest.raises(
        PeopleError, match="^You already have a group named Sample Crew.$"
    ):
        rename_group(db, other.id, "sample crew")
    assert [g.name for g, _ in list_groups(db)] == ["Sample Crew", "Test Crew"]


def test_the_index_itself_refuses_a_case_variant(db):
    # Not the service: the table, so the guarantee holds even if a check is skipped.
    db.add_all(
        [
            Person(name="Sam", created_at=FIXED_NOW),
            Person(name="sam", created_at=FIXED_NOW),
        ]
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- the people page ---------------------------------------------------------


def page(client) -> str:
    return html.unescape(client.get("/onboarding/people").text)


def post_person(client, name="Sam", **kwargs):
    return client.post("/onboarding/people/person", data={"name": name}, **kwargs)


def post_group(client, name="Sample Crew", members=(), **kwargs):
    data = {"name": name, "members": [str(m) for m in members]}
    return client.post("/onboarding/people/group", data=data, **kwargs)


def test_page_shows_the_step_and_both_buttons(client_at_people_step):
    text = page(client_at_people_step)
    assert "Step 4 of 5" in text
    assert "Continue" in text and "Skip for now" in text
    # One primary button, and it is Continue.
    assert text.count("em-btn--primary") == 1
    primary = re.search(r"<button[^>]*em-btn--primary[^>]*>.*?</button>", text, re.S)[0]
    assert "Continue" in primary
    secondary = re.search(
        r"<button[^>]*em-btn--secondary[^>]*>[^<]*<span[^>]*>Skip for now", text
    )
    assert secondary


def test_page_has_a_back_button_to_the_steam_step(client_at_people_step):
    text = page(client_at_people_step)
    back = re.search(r"<a [^>]*href=\"/onboarding/steam\"[^>]*>.*?</a>", text, re.S)
    assert back and "Back" in back[0]


def test_continue_and_skip_post_to_the_same_place(client_at_people_step):
    text = page(client_at_people_step)
    form = re.search(
        r"<form[^>]*action=\"/onboarding/people/done\"[^>]*>(.*?)</form>", text, re.S
    )
    assert form and "Continue" in form[1] and "Skip for now" in form[1]


def test_page_has_the_add_forms_and_ships_with_no_names(client_at_people_step, db):
    text = page(client_at_people_step)
    assert re.search(r"<form[^>]*action=\"/onboarding/people/person\"", text)
    assert re.search(r"<form[^>]*action=\"/onboarding/people/group\"", text)
    assert 'name="name"' in text
    assert 'class="people-list' not in text  # nobody listed yet
    assert "No one yet." in text
    assert list_people(db) == []


def test_without_people_the_group_form_says_to_add_people_first(client_at_people_step):
    text = page(client_at_people_step)
    assert "Add people first, then group them." in text
    assert 'name="members"' not in text and 'type="checkbox"' not in text


def test_the_forms_show_a_loading_state(client_at_people_step):
    text = page(client_at_people_step)
    assert text.count("hx-disabled-elt") >= 2
    assert 'hx-target="#people-section"' in text


def test_page_is_reachable_from_a_later_step(logged_in_client, db):
    advance_step(db, "tour")
    r = logged_in_client.get("/onboarding/people", follow_redirects=False)
    assert r.status_code == 200 and "Step 4 of 5" in r.text


def test_page_is_out_of_reach_from_an_earlier_step(client_at_steam_step):
    r = client_at_steam_step.get("/onboarding/people", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"


# --- adding a person ---------------------------------------------------------


def test_adding_a_person_lists_them(client_at_people_step, db):
    r = post_person(client_at_people_step, "  Sam ")
    assert r.status_code == 200
    assert "Sam" in html.unescape(r.text)
    assert names(db) == ["Sam"]
    assert "Sam" in page(client_at_people_step)


def test_adding_a_person_offers_them_in_the_group_form(client_at_people_step, db):
    post_person(client_at_people_step, "Sam")
    text = page(client_at_people_step)
    sam = list_people(db)[0]
    assert re.search(
        rf"<input[^>]*type=\"checkbox\"[^>]*name=\"members\"[^>]*value=\"{sam.id}\"",
        text,
    )
    assert "Add people first" not in text


def test_a_second_person_after_the_first(client_at_people_step, db):
    post_person(client_at_people_step, "Sam")
    post_person(client_at_people_step, "Alex")
    assert names(db) == ["Alex", "Sam"]


def test_a_new_person_starts_with_an_empty_field(client_at_people_step):
    text = post_person(client_at_people_step, "Sam").text
    field = re.search(r"<input[^>]*name=\"name\"[^>]*>", text)[0]
    assert "value=" not in field


@pytest.mark.parametrize(
    "name, message",
    [
        ("", "Enter a name."),
        ("   ", "Enter a name."),
        ("A" * 61, "Use 60 characters or fewer."),
    ],
)
def test_a_bad_name_is_explained_and_saves_nothing(
    client_at_people_step, db, name, message
):
    r = post_person(client_at_people_step, name)
    assert r.status_code == 200 and message in html.unescape(r.text)
    assert names(db) == []


def test_a_missing_name_field_is_a_blank_name_not_a_422(client_at_people_step):
    r = client_at_people_step.post("/onboarding/people/person", data={})
    assert r.status_code == 200 and "Enter a name." in r.text


def test_a_duplicate_person_is_explained_and_the_typed_name_stays(
    client_at_people_step, db
):
    post_person(client_at_people_step, "Sam")
    r = post_person(client_at_people_step, "sam")
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "You already have someone named Sam." in text
    assert 'value="sam"' in text
    assert names(db) == ["Sam"]


def test_a_too_long_name_stays_in_the_field_unaltered(client_at_people_step):
    name = "A" * 61
    text = post_person(client_at_people_step, name).text
    assert f'value="{name}"' in text


def test_names_are_escaped_everywhere_they_are_shown(client_at_people_step):
    name = "<script>alert(1)</script>"
    first = post_person(client_at_people_step, name).text
    assert "<script>alert(1)" not in first and "&lt;script&gt;alert(1)" in first
    # The duplicate message quotes the name too.
    again = post_person(client_at_people_step, name.upper()).text
    assert "<script>alert(1)" not in again.lower()
    assert (
        "You already have someone named &lt;script&gt;alert(1)&lt;/script&gt;." in again
    )
    # And so is the list on a fresh visit.
    assert (
        "<script>alert(1)" not in client_at_people_step.get("/onboarding/people").text
    )


def test_htmx_add_swaps_just_the_section_with_status_200(client_at_people_step):
    r = post_person(client_at_people_step, "Sam", headers={"HX-Request": "true"})
    text = html.unescape(r.text)
    assert r.status_code == 200
    assert "<html" not in text and 'id="people-section"' in text and "Sam" in text
    # The buttons that leave the step are not part of the section.
    assert "Continue" not in text


def test_htmx_error_swaps_just_the_section_with_status_200(client_at_people_step):
    r = post_person(client_at_people_step, " ", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text
    assert 'id="people-section"' in r.text and "Enter a name." in r.text


def test_the_full_page_carries_one_primary_button_even_with_an_error(
    client_at_people_step,
):
    text = post_person(client_at_people_step, "").text
    assert text.count("em-btn--primary") == 1


def test_adding_a_person_is_closed_before_this_step(client_at_steam_step, db):
    r = post_person(client_at_steam_step, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    assert names(db) == []


# --- adding a group ----------------------------------------------------------


@pytest.fixture
def two_people(client_at_people_step, db):
    post_person(client_at_people_step, "Sam")
    post_person(client_at_people_step, "Alex")
    return client_at_people_step, {p.name: p.id for p in list_people(db)}


def test_adding_a_group_lists_it_with_its_members(two_people, db):
    client, ids = two_people
    r = post_group(client, "  Sample Crew ", [ids["Sam"], ids["Alex"]])
    text = html.unescape(r.text)
    assert r.status_code == 200 and "Sample Crew" in text
    [(group, members)] = list_groups(db)
    assert group.name == "Sample Crew" and [p.name for p in members] == ["Alex", "Sam"]
    listing = re.search(r"Sample Crew.*", page(client), re.S)[0]
    assert "Sam" in listing and "Alex" in listing


def test_a_group_with_no_members_is_listed_as_empty(two_people, db):
    client, _ = two_people
    text = post_group(client, "Sample Crew").text
    assert "Sample Crew" in text and "No members yet." in text
    assert [(g.name, m) for g, m in list_groups(db)] == [("Sample Crew", [])]


def test_a_new_group_starts_with_empty_fields(two_people):
    client, ids = two_people
    text = post_group(client, "Sample Crew", [ids["Sam"]]).text
    assert "checked" not in text
    assert 'value="Sample Crew"' not in text


@pytest.mark.parametrize(
    "name, message",
    [
        ("", "Enter a name."),
        ("  ", "Enter a name."),
        ("G" * 61, "Use 60 characters or fewer."),
    ],
)
def test_a_bad_group_name_keeps_the_ticked_members(two_people, db, name, message):
    client, ids = two_people
    r = post_group(client, name, [ids["Sam"]])
    text = html.unescape(r.text)
    assert r.status_code == 200 and message in text
    sam_box = re.search(rf"<input[^>]*value=\"{ids['Sam']}\"[^>]*>", text)[0]
    alex_box = re.search(rf"<input[^>]*value=\"{ids['Alex']}\"[^>]*>", text)[0]
    assert "checked" in sam_box and "checked" not in alex_box
    assert list_groups(db) == []


def test_a_duplicate_group_is_explained_and_the_typed_name_stays(two_people, db):
    client, ids = two_people
    post_group(client, "Sample Crew", [ids["Sam"]])
    r = post_group(client, "sample crew", [ids["Alex"]])
    text = html.unescape(r.text)
    assert "You already have a group named Sample Crew." in text
    assert 'value="sample crew"' in text
    [(_, members)] = list_groups(db)
    assert [p.name for p in members] == ["Sam"]  # the refused attempt changed nothing


@pytest.mark.parametrize("member", ["999", "abc", "", "-1", "1.5", str(10**30)])
def test_an_unknown_member_is_refused_with_nothing_saved(two_people, db, member):
    client, ids = two_people
    r = client.post(
        "/onboarding/people/group",
        data={"name": "Sample Crew", "members": [str(ids["Sam"]), member]},
    )
    assert r.status_code == 200
    assert "Pick people from your list." in r.text
    assert list_groups(db) == []


def test_htmx_group_swaps_just_the_section(two_people):
    client, ids = two_people
    r = post_group(client, "Sample Crew", [ids["Sam"]], headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="people-section"' in r.text
    assert "Sample Crew" in r.text


def test_a_group_name_is_escaped(two_people):
    client, ids = two_people
    text = post_group(client, "<b>Crew</b>", [ids["Sam"]]).text
    assert "<b>Crew</b>" not in text and "&lt;b&gt;Crew&lt;/b&gt;" in text


def test_adding_a_group_is_closed_before_this_step(client_at_steam_step, db):
    r = post_group(client_at_steam_step, follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    assert list_groups(db) == []


# --- leaving the step --------------------------------------------------------


def test_continue_moves_on_to_the_tour(client_at_people_step, db):
    post_person(client_at_people_step, "Sam")
    r = client_at_people_step.post("/onboarding/people/done", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/tour"
    assert get_setting(db, "onboarding_step") == "tour"
    assert names(db) == ["Sam"]


def test_skip_has_the_same_effect_with_nothing_added(client_at_people_step, db):
    # "Skip for now" posts to the same route as Continue.
    r = client_at_people_step.post("/onboarding/people/done", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding/tour"
    assert get_setting(db, "onboarding_step") == "tour"
    assert names(db) == [] and list_groups(db) == []


def test_htmx_done_redirects_the_whole_page(client_at_people_step):
    r = client_at_people_step.post(
        "/onboarding/people/done",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/onboarding/tour"


def test_done_never_rewinds(logged_in_client, db):
    advance_step(db, "done")
    r = logged_in_client.post("/onboarding/people/done", follow_redirects=False)
    # A finished user is sent home by the guard, and the step stays done.
    assert r.headers["location"] == "/"
    assert get_setting(db, "onboarding_step") == "done"


def test_coming_back_to_this_step_from_the_tour_keeps_the_tour_step(
    logged_in_client, db
):
    advance_step(db, "tour")
    r = logged_in_client.post("/onboarding/people/done", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/tour"
    assert get_setting(db, "onboarding_step") == "tour"


def test_done_is_closed_before_this_step(client_at_steam_step, db):
    r = client_at_steam_step.post("/onboarding/people/done", follow_redirects=False)
    assert r.headers["location"] == "/onboarding/steam"
    assert get_setting(db, "onboarding_step") == "steam"


# --- parse_member_ids ----------------------------------------------------------


def test_parse_member_ids_reads_whole_numbers():
    assert parse_member_ids(["3", "1", "3"]) == ([3, 1, 3], True)
    assert parse_member_ids([]) == ([], True)


@pytest.mark.parametrize("bad", ["", "x", "-1", "1.5", " 2", "٣", "1e3"])
def test_parse_member_ids_flags_anything_else(bad):
    # Only a request made outside the form can send one of these. The numbers that were
    # fine still come back; the flag says the list can't be trusted.
    assert parse_member_ids(["7", bad]) == ([7], False)


def test_parse_member_ids_takes_a_huge_number_as_a_number():
    # Whether a person has that id is the service's call; it must not overflow here.
    assert parse_member_ids([str(2**80)]) == ([2**80], True)
