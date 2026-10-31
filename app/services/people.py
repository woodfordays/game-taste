"""People and named groups: the names a review can say it was played with.

Names are unique ignoring case, separately for people and for groups. The database
enforces that with a unique index on `lower(name)`; the checks here come first so the
message can name the person or group already saved. The messages can quote what the
user typed, so they are plain strings and the templates escape them.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GroupMember, NamedGroup, Person

NAME_MAX = 60

NAME_MISSING = "Enter a name."
NAME_TOO_LONG = f"Use {NAME_MAX} characters or fewer."
PICK_PEOPLE = "Pick people from your list."
PERSON_MISSING = "Couldn't find that person. Reload the page and try again."
GROUP_MISSING = "Couldn't find that group. Reload the page and try again."

_TAKEN = {
    Person: "You already have someone named {}.",
    NamedGroup: "You already have a group named {}.",
}


class PeopleError(Exception):
    """Something the user can fix. The message says what happened and what to do."""


def _clean_name(raw: str) -> str:
    """`raw` without the whitespace around it. Inner whitespace stays as typed."""
    name = raw.strip()
    if not name:
        raise PeopleError(NAME_MISSING)
    if len(name) > NAME_MAX:
        raise PeopleError(NAME_TOO_LONG)
    return name


def _find_by_name(
    db: Session, model: type[Person] | type[NamedGroup], name: str, *, ignore_id=None
):
    """The saved person or group whose name matches `name` ignoring case, or None.

    `ignore_id` leaves one row out, so renaming something to a new spelling of its own
    name is not a clash. The match is a little wider than the database index (it also
    folds non-ASCII letters), never narrower.
    """
    key = name.casefold()
    for row in db.scalars(select(model)):
        if row.id != ignore_id and row.name.casefold() == key:
            return row
    return None


def _check_free(db, model, name: str, *, ignore_id=None) -> None:
    existing = _find_by_name(db, model, name, ignore_id=ignore_id)
    if existing is not None:
        raise PeopleError(_TAKEN[model].format(existing.name))


def _taken_after_conflict(
    db: Session, model, name: str, conflict: IntegrityError
) -> PeopleError:
    """The error for a name the unique index refused although the check had passed
    (another request saved it in between). Call it after rolling back. If no row has the
    name, the index wasn't the problem, so the original error goes on."""
    existing = _find_by_name(db, model, name)
    if existing is None:
        raise conflict
    return PeopleError(_TAKEN[model].format(existing.name))


def parse_member_ids(values: Iterable[object]) -> tuple[list[int], bool]:
    """The ids in the ticked checkbox values, and whether every value was a whole number.

    A value that isn't one can only come from outside the form. The ids that did parse
    still come back. Whether each id is a person is for the add and set functions to say.
    """
    ids: list[int] = []
    clean = True
    for value in values:
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            try:
                ids.append(int(value))
            except ValueError:
                # Python won't convert a string of more than 4300 digits. No person has
                # such an id, so it is a value that isn't one.
                clean = False
        else:
            clean = False
    return ids, clean


def _member_ids(db: Session, member_ids: list[int]) -> list[int]:
    """`member_ids` without repeats, in the order given, once each is known to be a person."""
    unique = list(dict.fromkeys(member_ids))
    known = set(db.scalars(select(Person.id)))
    if not all(person_id in known for person_id in unique):
        raise PeopleError(PICK_PEOPLE)
    return unique


# --- people ---------------------------------------------------------------------


def add_person(db: Session, name: str, now: datetime) -> Person:
    name = _clean_name(name)
    _check_free(db, Person, name)
    person = Person(name=name, created_at=now)
    db.add(person)
    try:
        db.commit()
    except IntegrityError as conflict:
        db.rollback()
        raise _taken_after_conflict(db, Person, name, conflict) from None
    return person


def rename_person(db: Session, person_id: int, name: str) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise PeopleError(PERSON_MISSING)
    name = _clean_name(name)
    _check_free(db, Person, name, ignore_id=person_id)
    person.name = name
    try:
        db.commit()
    except IntegrityError as conflict:
        db.rollback()
        raise _taken_after_conflict(db, Person, name, conflict) from None
    return person


def list_people(db: Session) -> list[Person]:
    """Everyone, by name (ignoring case)."""
    return list(db.scalars(select(Person).order_by(func.lower(Person.name), Person.id)))


# --- named groups ---------------------------------------------------------------


def add_group(
    db: Session, name: str, member_ids: list[int], now: datetime
) -> NamedGroup:
    """Save a group with the given members. Nothing is saved if the name or any member is refused."""
    name = _clean_name(name)
    _check_free(db, NamedGroup, name)
    members = _member_ids(db, member_ids)
    group = NamedGroup(name=name, created_at=now)
    try:
        db.add(group)
        db.flush()  # gives the group its id for the member rows
        db.add_all(
            GroupMember(group_id=group.id, person_id=person_id) for person_id in members
        )
        db.commit()
    except IntegrityError as conflict:
        db.rollback()
        raise _taken_after_conflict(db, NamedGroup, name, conflict) from None
    return group


def rename_group(db: Session, group_id: int, name: str) -> NamedGroup:
    group = db.get(NamedGroup, group_id)
    if group is None:
        raise PeopleError(GROUP_MISSING)
    name = _clean_name(name)
    _check_free(db, NamedGroup, name, ignore_id=group_id)
    group.name = name
    try:
        db.commit()
    except IntegrityError as conflict:
        db.rollback()
        raise _taken_after_conflict(db, NamedGroup, name, conflict) from None
    return group


def set_group_members(db: Session, group_id: int, member_ids: list[int]) -> None:
    """Make `member_ids` the group's members, replacing the current ones.

    Reviews already published keep the people they were tagged with, so this changes
    only who the group stands for from now on.
    """
    if db.get(NamedGroup, group_id) is None:
        raise PeopleError(GROUP_MISSING)
    wanted = _member_ids(db, member_ids)
    current = set(group_member_ids(db, group_id))
    gone = current - set(wanted)
    if gone:
        db.execute(
            delete(GroupMember).where(
                GroupMember.group_id == group_id, GroupMember.person_id.in_(gone)
            )
        )
    db.add_all(
        GroupMember(group_id=group_id, person_id=person_id)
        for person_id in wanted
        if person_id not in current
    )
    db.commit()


def group_member_ids(db: Session, group_id: int) -> list[int]:
    return list(
        db.scalars(
            select(GroupMember.person_id)
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.person_id)
        )
    )


def list_groups(db: Session) -> list[tuple[NamedGroup, list[Person]]]:
    """Every group, by name (ignoring case), with its members by name."""
    members: dict[int, list[Person]] = defaultdict(list)
    rows = db.execute(
        select(GroupMember.group_id, Person)
        .join(Person, Person.id == GroupMember.person_id)
        .order_by(func.lower(Person.name), Person.id)
    )
    for group_id, person in rows:
        members[group_id].append(person)
    groups = db.scalars(
        select(NamedGroup).order_by(func.lower(NamedGroup.name), NamedGroup.id)
    )
    return [(group, members[group.id]) for group in groups]
