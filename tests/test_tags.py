import pytest

from app.services.tags import normalize_tags

# --- the brief's test --------------------------------------------------------


def test_normalize_tags():
    assert normalize_tags(
        "#deck builder, Roguelite, number  go up, roguelite, , ##Co Op"
    ) == [
        "deck_builder",
        "roguelite",
        "number_go_up",
        "co_op",
    ]


# --- edge cases --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,, ", "#", "# #", "##,#"])
def test_nothing_usable_gives_no_tags(raw):
    assert normalize_tags(raw) == []


def test_space_after_the_hash_is_not_kept():
    assert normalize_tags("# deck builder") == ["deck_builder"]


def test_a_hash_inside_a_tag_stays():
    assert normalize_tags("c#, a#b") == ["c#", "a#b"]


def test_any_whitespace_run_becomes_one_underscore():
    assert normalize_tags("number \t\n  go up") == ["number_go_up"]


def test_duplicates_ignore_case_and_hashes_and_keep_the_first_position():
    assert normalize_tags("Rogue, #ROGUE, Cozy, rogue ,cozy") == ["rogue", "cozy"]


def test_order_is_the_order_typed():
    assert normalize_tags("zeta, alpha, mid") == ["zeta", "alpha", "mid"]


def test_non_ascii_letters_are_lowercased_too():
    assert normalize_tags("Über Hard") == ["über_hard"]


def test_a_new_list_each_call():
    first = normalize_tags("a")
    first.append("b")
    assert normalize_tags("a") == ["a"]
