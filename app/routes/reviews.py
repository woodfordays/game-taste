"""The review dialog: opening it on a lane, saving a draft or publishing, switching lanes,
and the two small answers its fields ask for (tag chips and who played).

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
from app.models import Game
from app.routes.library import MAX_APPID
from app.services.people import PICK_PEOPLE, list_groups, list_people
from app.services.reviews import (
    LANE_LABELS,
    OPTION_INVALID,
    RATING_INVALID,
    Lane,
    LaneState,
    ReviewError,
    ReviewForm,
    default_lane,
    effective_people,
    head_count_hint,
    lane_states,
    load_form,
    publish,
    save_draft,
)
from app.services.tags import normalize_tags
from app.web import read_form, render

router = APIRouter()

AppId = Annotated[int, Path(ge=1, le=MAX_APPID)]

ACTIONS = ("draft", "publish")
SWITCH_CHOICES = ("", "save", "discard")


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


def _ids(values: list) -> list[int]:
    """Whole numbers from posted values, ignoring blanks. `ValueError` for anything else."""
    return [int(value) for value in values if isinstance(value, str) and value.strip()]


def form_from_request(form: FormData) -> ReviewForm:
    """The `ReviewForm` for a posted dialog. Nothing but numbers is ever converted.

    The tags input is named `tags` (it becomes `tags_raw`), empty choices are None, and
    text (`text`, `group_text`, `session_length`, `tags`) is passed through exactly as
    posted. A rating, a person or a group that isn't a whole number raises
    `FormInputError` (the rating's message first) with everything else that was read, so
    the dialog can show it again. Whether a number is a valid rating, person or group is
    for the service to say.
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
    try:
        review.people = _ids(form.getlist("people"))
    except ValueError:
        problems.append(PICK_PEOPLE)
    try:
        group = _text(form, "named_group_id")
        review.named_group_id = int(group) if group.strip() else None
    except ValueError:
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


def _who_played(
    db: Session, lane: Lane | None, people: list[int], group_id: int | None
) -> tuple[set[int], str | None]:
    """The ids to tick, and the head-count hint for `lane` (none without a lane).

    The ticked people and the named group's members all count, once each."""
    everyone = effective_people(db, people, group_id)
    hint = None if lane is None else head_count_hint(lane, len(everyone))
    return set(everyone), hint


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
    selected, hint = (
        _who_played(db, lane, form.people, form.named_group_id)
        if coop
        else (set(), None)
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
        selected=selected,
        hint=hint,
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
    already ticked, and the head-count hint for `lane`."""
    try:
        ticked = _ids(people)
        group_id = int(named_group_id) if named_group_id.strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail=PICK_PEOPLE) from None
    selected, hint = _who_played(db, lane, ticked, group_id)
    return render(
        request,
        "partials/people_options.html",
        people=list_people(db),
        selected=selected,
        lane=lane,
        hint=hint,
    )
