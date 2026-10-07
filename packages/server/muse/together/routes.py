from typing import Annotated, Any

from dishka.integrations.litestar import FromDishka, inject
from litestar import Request, Router, delete, get, post, put
from litestar.params import FromPath, FromQuery, QueryParameter

from muse.catalog.domain import NO_TRACK
from muse.identity.domain import User
from muse.shared.errors import domain_errors
from muse.together.dates import DatesView
from muse.together.domain import (
    BAD_PIN,
    BAD_TEXT,
    FOLLOW_WAIT_S,
    NO_LETTER,
    NO_WAITING,
    PARTNER_FOLLOWS,
    FollowView,
    LetterList,
    LiveView,
    NotesView,
    OursView,
    PinsView,
    SecretList,
    SecretView,
)
from muse.together.schemas import LetterBody, LiveBody, PinBody
from muse.together.service import Dates, Following, Letters, Live, Notes, Ours, Pins, Secrets

ERRORS = {
    NO_TRACK: 404,
    PARTNER_FOLLOWS: 409,
    BAD_TEXT: 422,
    BAD_PIN: 422,
    NO_LETTER: 404,
    NO_WAITING: 404,
}


@post("/live", status_code=200)
@inject
async def report_beat(
    data: LiveBody, request: Request[User, str, Any], live: FromDishka[Live]
) -> LiveView:
    return await live.beat(request.user.id, data.track_id, data.position, data.playing)


@get("/live")
@inject
async def live_view(request: Request[User, str, Any], live: FromDishka[Live]) -> LiveView:
    return await live.view(request.user.id)


@get("/live/follow")
@inject
async def follow(
    request: Request[User, str, Any],
    following: FromDishka[Following],
    since: FromQuery[int] = 0,
    wait: Annotated[float, QueryParameter(ge=0, le=FOLLOW_WAIT_S)] = FOLLOW_WAIT_S,
) -> FollowView:
    return await following.follow(request.user.id, since, wait)


@delete("/live/follow", status_code=204)
@inject
async def unfollow(request: Request[User, str, Any], following: FromDishka[Following]) -> None:
    following.unfollow(request.user.id)


@post("/letters", status_code=201)
@inject
async def send_letter(
    data: LetterBody, request: Request[User, str, Any], letters: FromDishka[Letters]
) -> dict[str, int]:
    return await letters.send(request.user.id, data.track_id, data.text)


@get("/letters")
@inject
async def letters_view(
    request: Request[User, str, Any], letters: FromDishka[Letters]
) -> LetterList:
    return await letters.listing(request.user.id)


@post("/letters/{lid:int}/read", status_code=204)
@inject
async def read_letter(
    lid: FromPath[int], request: Request[User, str, Any], letters: FromDishka[Letters]
) -> None:
    await letters.mark_read(request.user.id, lid)


@get("/ours")
@inject
async def ours_view(ours: FromDishka[Ours]) -> OursView:
    return await ours.listing()


@put("/ours/{tid:int}", status_code=204)
@inject
async def add_ours(
    tid: FromPath[int], request: Request[User, str, Any], ours: FromDishka[Ours]
) -> None:
    await ours.add(request.user.id, tid)


@delete("/ours/{tid:int}", status_code=204)
@inject
async def remove_ours(
    tid: FromPath[int], request: Request[User, str, Any], ours: FromDishka[Ours]
) -> None:
    await ours.remove(request.user.id, tid)


@get("/notes")
@inject
async def notes_view(request: Request[User, str, Any], notes: FromDishka[Notes]) -> NotesView:
    return await notes.daily(request.user.id)


@post("/secrets", status_code=201)
@inject
async def hide_secret(
    data: LetterBody, request: Request[User, str, Any], secrets: FromDishka[Secrets]
) -> dict[str, int]:
    return await secrets.hide(request.user.id, data.track_id, data.text)


@post("/secrets/find/{tid:int}", status_code=200)
@inject
async def find_secret(
    tid: FromPath[int], request: Request[User, str, Any], secrets: FromDishka[Secrets]
) -> dict[str, SecretView | None]:
    return await secrets.find(request.user.id, tid)


@get("/secrets")
@inject
async def secrets_view(
    request: Request[User, str, Any], secrets: FromDishka[Secrets]
) -> SecretList:
    return await secrets.listing(request.user.id)


@delete("/secrets/{sid:int}", status_code=204)
@inject
async def drop_secret(
    sid: FromPath[int], request: Request[User, str, Any], secrets: FromDishka[Secrets]
) -> None:
    await secrets.drop(request.user.id, sid)


@put("/pin", status_code=204)
@inject
async def set_pin(data: PinBody, request: Request[User, str, Any], pins: FromDishka[Pins]) -> None:
    await pins.set(request.user.id, data.track_id, data.text)


@delete("/pin", status_code=204)
@inject
async def unset_pin(request: Request[User, str, Any], pins: FromDishka[Pins]) -> None:
    await pins.remove(request.user.id)


@get("/pin")
@inject
async def pin_view(request: Request[User, str, Any], pins: FromDishka[Pins]) -> PinsView:
    return await pins.view(request.user.id)


@get("/dates")
@inject
async def dates_view(dates_service: FromDishka[Dates]) -> DatesView:
    return await dates_service.view()


router = Router(
    "/api",
    route_handlers=[
        report_beat,
        live_view,
        follow,
        unfollow,
        send_letter,
        letters_view,
        read_letter,
        ours_view,
        add_ours,
        remove_ours,
        notes_view,
        hide_secret,
        find_secret,
        secrets_view,
        drop_secret,
        set_pin,
        unset_pin,
        pin_view,
        dates_view,
    ],
    exception_handlers=domain_errors(ERRORS),
)
