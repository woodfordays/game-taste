"""The review dialog: opening it on a lane, saving a draft or publishing, switching lanes,
and the two small answers its fields ask for (tag chips and who played). Also the Reviews
page, a table of every review, and a lane's read-only history of published versions.

Every answer is a fragment for htmx to swap into `#dialog`, and a refused form comes back
as the same dialog with status 200, because htmx doesn't swap an error status. A save that
works answers with an empty body, which clears `#dialog` and so closes the dialog, and an
`HX-Trigger` event that tells the game's Home card to refresh.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.models import Game, NamedGroup, ReviewVersion
from app.routes.library import MAX_APPID
from app.services.people import (
    PICK_PEOPLE,
    group_member_ids,
    list_groups,
    list_people,
)
from app.services.reviews import (
    CHOICE_OPTIONS,
    LANE_LABELS,
    OPTION_INVALID,
    RATING_INVALID,
    VERDICTS,
    Lane,
    LaneState,
    ReviewError,
    ReviewForm,
    default_lane,
    effective_people,
    head_count_hint,
    history,
    lane_states,
    list_reviews,
    load_form,
    publish,
    save_draft,
)
from app.services.tags import normalize_tags
from app.web import is_htmx, parse_id, read_form, render

router = APIRouter()

AppId = Annotated[int, Path(ge=1, le=MAX_APPID)]

ACTIONS = ("draft", "publish")
SWITCH_CHOICES = ("", "save", "discard")

# The Reviews page's filters: (value, label), the empty value meaning no filter.
LANE_OPTIONS = [("", "All lanes"), *((lane.value, LANE_LABELS[lane]) for lane in Lane)]
VERDICT_OPTIONS = [("", "All verdicts"), *((v, v.capitalize()) for v in VERDICTS)]


class FormInputError(ReviewError):
    """A posted value that is not a number where one is needed.

    `form` holds everything that could be read, so the dialog can show it again.
    """

    def __init__(self, message: str, form: ReviewForm) -> None:
        super().__init__(message)
        self.form = form


def _text(form: FormData, name: str) -> str:
    """One field exactly as posted. A missing field, or a file, is empty text."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _choice(form: FormData, name: str) -> str | None:
    """A choice field: the empty choice is None. Whether the rest is on the list is the
    service's call."""
    return _text(form, name) or None


def _ids(values: list) -> list[int] | None:
    """The ids in posted values, ignoring blanks. None if any other value isn't an id
    (see `parse_id`)."""
    ids = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = parse_id(value)
        if parsed is None:
            return None
        ids.append(parsed)
    return ids


def form_from_request(form: FormData) -> ReviewForm:
    """The `ReviewForm` for a posted dialog. Nothing but numbers is ever converted.

    The tags input is named `tags` (it becomes `tags_raw`), empty choices are None, and
    text (`text`, `group_text`, `session_length`, `tags`) is passed through exactly as
    posted. A rating that isn't a whole number, or a person or a group that isn't a usable
    id (`parse_id`), raises `FormInputError` (the rating's message first) with everything
    else that was read, so the dialog can show it again. Whether a rating is from 1 to 10,
    and whether a person or group exists, is for the service to say.
    """
    review = ReviewForm(
        text=_text(form, "text"),
        tags_raw=_text(form, "tags"),
        group_text=_text(form, "group_text"),
        session_length=_text(form, "session_length"),
        status=_choice(form, "status"),
        verdict=_choice(form, "verdict"),
        walk_away=_choice(form, "walk_away"),
        complexity=_choice(form, "complexity"),
    )
    problems = []
    try:
        rating = _text(form, "rating")
        review.rating = int(rating) if rating else None
    except ValueError:
        problems.append(RATING_INVALID)
    people = _ids(form.getlist("people"))
    if people is None:
        problems.append(PICK_PEOPLE)
    else:
        review.people = people
    group = _text(form, "named_group_id")
    if group.strip():
        review.named_group_id = parse_id(group)
        if review.named_group_id is None:
            problems.append(PICK_PEOPLE)
    if problems:
        raise FormInputError(problems[0], review)
    return review


def _parse_lane(value: str) -> Lane | None:
    try:
        return Lane(value)
    except ValueError:
        return None


def _game(db: Session, appid: int) -> Game:
    game = db.get(Game, appid)
    if game is None:
        raise HTTPException(status_code=404, detail="No such game.")
    return game


def _card_event(appid: int) -> dict[str, str]:
    """The header that tells the game's Home card to refresh."""
    return {"HX-Trigger": f"card-updated-{appid}"}


@dataclass
class LaneTab:
    lane: Lane
    label: str
    mark: str | None  # "Draft" or "Reviewed", for a lane that has either
    current: bool


def _lane_tabs(states: dict[Lane, LaneState], current: Lane) -> list[LaneTab]:
    """The lane switcher's buttons. A lane with a draft says Draft, even when it also has
    a published version, because the draft is what the lane opens on."""
    return [
        LaneTab(
            lane,
            LANE_LABELS[lane],
            "Draft" if state.has_draft else "Reviewed" if state.has_published else None,
            lane is current,
        )
        for lane, state in states.items()
    ]


@dataclass
class WhoPlayed:
    """What the "who played" checkboxes show."""

    # The ids to tick: the people ticked and the named group's members.
    selected: set[int]
    # The group's members: ticked and not editable, since the group counts them.
    locked: set[int]
    group: NamedGroup | None  # the group chosen, for its name in the hint
    hint: str | None  # the head-count hint


def _who_played(
    db: Session, lane: Lane | None, people: list[int], group_id: int | None
) -> WhoPlayed:
    """The ticks, the locked members and the head-count hint for `lane` (none without a lane).

    The ticked people and the named group's members all count, once each. The group's
    members are shown ticked and locked, so the page says what the server does: they play
    whatever is ticked, and a locked box isn't posted, so `people` holds only the extras.
    A group that doesn't exist locks nobody."""
    group = None if group_id is None else db.get(NamedGroup, group_id)
    everyone = effective_people(db, people, group_id)
    return WhoPlayed(
        selected=set(everyone),
        locked=set() if group is None else set(group_member_ids(db, group.id)),
        group=group,
        hint=None if lane is None else head_count_hint(lane, len(everyone)),
    )


def _has_details(form: ReviewForm, coop: bool) -> bool:
    """Whether the form has anything in "More details" (so the section opens)."""
    filled = [
        form.status,
        form.verdict,
        form.walk_away,
        form.complexity,
        form.session_length,
    ]
    if coop:
        filled += [form.people, form.named_group_id, form.group_text]
    return any(filled)


def _note(state: LaneState) -> str | None:
    """A line under the lane switcher about what the lane already holds."""
    if state.has_draft:
        return "This is your saved draft for this lane."
    if state.has_published:
        return "Publishing again adds a new version and keeps the earlier ones."
    return None


def _dialog(
    request: Request,
    db: Session,
    game: Game,
    lane: Lane,
    form: ReviewForm,
    *,
    error: str | None = None,
    switch_to: Lane | None = None,
    dirty: bool = False,
    still: bool = True,
    refresh_card: bool = False,
):
    """The dialog for `lane` showing `form`.

    `error` is shown above the fields. `switch_to` adds the "unsaved changes" question
    about moving to that lane. `dirty` is whether `form` holds changes that aren't saved.
    `still` stops the dialog's entrance animation, since it is already on screen: only the
    first opening animates. `refresh_card` adds the event that refreshes the Home card.
    """
    coop = lane is not Lane.SOLO
    who = (
        _who_played(db, lane, form.people, form.named_group_id)
        if coop
        else WhoPlayed(set(), set(), None, None)
    )
    states = lane_states(db, game.appid)
    response = render(
        request,
        "partials/review_dialog.html",
        game=game,
        lane=lane,
        tabs=_lane_tabs(states, lane),
        note=_note(states[lane]),
        form=form,
        tags=normalize_tags(form.tags_raw),
        error=error,
        switch_to=switch_to,
        dirty="1" if dirty else "0",
        still=still,
        coop=coop,
        people=list_people(db),
        groups=list_groups(db) if coop else [],
        selected=who.selected,
        locked=who.locked,
        chosen_group=who.group,
        hint=who.hint,
        choices=CHOICE_OPTIONS,
        details_open=_has_details(form, coop) or error in (OPTION_INVALID, PICK_PEOPLE),
    )
    if refresh_card:
        response.headers.update(_card_event(game.appid))
    return response


@router.get("/games/{appid}/review")
def review_dialog(
    appid: AppId,
    request: Request,
    lane: str = "",
    db: Session = Depends(get_db),
):
    """The dialog for a game, on `lane` or, without one (or with a name that isn't a
    lane), on the lane worked on most recently."""
    game = _game(db, appid)
    chosen = _parse_lane(lane) or default_lane(db, appid)
    form, _source = load_form(db, appid, chosen)
    return _dialog(request, db, game, chosen, form, still=False)


@router.post("/games/{appid}/review/{lane}")
def review_save(
    appid: AppId,
    lane: Lane,
    request: Request,
    data: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Save the lane's draft or publish it, by the `action` field."""
    game = _game(db, appid)
    action = _text(data, "action")
    if action not in ACTIONS:
        raise HTTPException(
            status_code=400, detail='"action" must be draft or publish.'
        )
    try:
        form = form_from_request(data)
    except FormInputError as bad:
        return _dialog(request, db, game, lane, bad.form, error=str(bad), dirty=True)
    now = request.app.state.services.clock()
    try:
        if action == "draft":
            save_draft(db, appid, lane, form, now)
        else:
            publish(db, appid, lane, form, now)
    except ReviewError as failure:
        return _dialog(request, db, game, lane, form, error=str(failure), dirty=True)
    return HTMLResponse("", headers=_card_event(appid))


@router.post("/games/{appid}/review/{lane}/switch")
def review_switch(
    appid: AppId,
    lane: Lane,
    request: Request,
    data: FormData = Depends(read_form),
    db: Session = Depends(get_db),
):
    """Move the dialog from `lane` to the lane in `to`.

    With no changes (`dirty` is "0") it just opens that lane. With changes it asks first,
    and `choice` answers: "save" keeps them as the current lane's draft, "discard" drops
    them. A missing `dirty` counts as changes, so nothing typed is lost to a bad request.
    """
    game = _game(db, appid)
    target = _parse_lane(_text(data, "to"))
    choice = _text(data, "choice")
    if target is None or choice not in SWITCH_CHOICES:
        raise HTTPException(
            status_code=400, detail="Pick a lane and a choice to switch."
        )

    if _text(data, "dirty") == "0" or choice == "discard":
        form, _source = load_form(db, appid, target)
        return _dialog(request, db, game, target, form)

    try:
        form = form_from_request(data)
    except FormInputError as bad:
        return _dialog(request, db, game, lane, bad.form, error=str(bad), dirty=True)
    if target is lane:
        return _dialog(request, db, game, lane, form, dirty=True)
    if choice == "":
        return _dialog(request, db, game, lane, form, switch_to=target, dirty=True)

    try:
        save_draft(db, appid, lane, form, request.app.state.services.clock())
    except ReviewError as failure:
        return _dialog(request, db, game, lane, form, error=str(failure), dirty=True)
    saved, _source = load_form(db, appid, target)
    return _dialog(request, db, game, target, saved, refresh_card=True)


@router.get("/tags/preview")
def tag_preview(request: Request, raw: str = "", tags: str = ""):
    """The chips for a typed tag list. The dialog's input is named `tags`, so that
    works as well as `raw`."""
    return render(request, "partials/tag_chips.html", tags=normalize_tags(raw or tags))


@router.get("/people/options")
def people_options(
    request: Request,
    lane: Lane | None = None,
    named_group_id: str = "",
    people: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
):
    """The "who played" checkboxes with the named group's members added to the people
    already ticked (and locked, since the group counts them), and the head-count hint for
    `lane`. `people` is only the extras: a locked box isn't sent.

    A person that isn't a usable id, or a group that isn't a number, is a bad request. A
    group number that no group could have (zero, or past what the database holds) is a
    group that doesn't exist, like any other number nobody has: it adds no one."""
    ticked = _ids(people)
    group_text = named_group_id.strip()
    group_id = parse_id(group_text)
    if ticked is None or (group_text and group_id is None and not group_text.isdigit()):
        raise HTTPException(status_code=400, detail=PICK_PEOPLE)
    who = _who_played(db, lane, ticked, group_id)
    return render(
        request,
        "partials/people_options.html",
        people=list_people(db),
        selected=who.selected,
        locked=who.locked,
        chosen_group=who.group,
        lane=lane,
        hint=who.hint,
    )


@router.get("/reviews")
def reviews_page(
    request: Request,
    lane: str = "",
    verdict: str = "",
    db: Session = Depends(get_db),
):
    """The table of every review, one row for each game and lane with a draft or a
    published version, newest first. `lane` and `verdict` filter it, and a value that
    isn't a lane or a verdict filters nothing, so an old link never errors.

    htmx asks for just the table. Its history restore wants the whole page, since it
    refetches a URL it pushed once it has no snapshot of it, and the two answers share
    a URL, so `Vary` keeps a cache from handing one out for the other.
    """
    chosen = _parse_lane(lane)
    kept = verdict if verdict in VERDICTS else ""
    fragment = (
        is_htmx(request) and request.headers.get("hx-history-restore-request") != "true"
    )
    response = render(
        request,
        "partials/reviews_table.html" if fragment else "reviews.html",
        nav="reviews",
        rows=list_reviews(db, chosen, kept or None),
        lane=chosen.value if chosen else "",
        verdict=kept,
        lane_options=LANE_OPTIONS,
        verdict_options=VERDICT_OPTIONS,
    )
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


@dataclass
class HistoryEntry:
    version: ReviewVersion
    played_with: list[str]  # names, in alphabetical order


@router.get("/games/{appid}/review/{lane}/history")
def review_history(
    appid: AppId,
    lane: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Every published version of a game's lane review, newest first, as it was
    published. Nothing here can be changed."""
    game = _game(db, appid)
    chosen = _parse_lane(lane)
    if chosen is None:
        raise HTTPException(status_code=404, detail="No such lane.")
    names = {person.id: person.name for person in list_people(db)}
    entries = [
        HistoryEntry(
            version,
            sorted(
                (names[p.person_id] for p in version.people if p.person_id in names),
                key=str.casefold,
            ),
        )
        for version in history(db, appid, chosen)
    ]
    return render(
        request, "history.html", nav="reviews", game=game, lane=chosen, entries=entries
    )
