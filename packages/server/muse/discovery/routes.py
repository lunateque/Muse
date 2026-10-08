from typing import Annotated, Any

from dishka.integrations.litestar import FromDishka, inject
from litestar import Request, Router, get
from litestar.params import FromPath, FromQuery, QueryParameter

from muse.catalog.domain import NO_SUCH_GENRE, NO_TRACK
from muse.discovery.domain import (
    NO_MOOD,
    RADIO_MAX,
    RADIO_MIN,
    RADIO_SIZE,
    SEARCH_DOWN,
    SEARCH_MAX_CHARS,
    Mix,
    MoodGroups,
    MoodMix,
    RadioView,
    excluded_list,
)
from muse.discovery.service import Mixes, Moods, Radio
from muse.identity.domain import User
from muse.shared.errors import domain_errors

ERRORS = {NO_TRACK: 404, NO_SUCH_GENRE: 404, NO_MOOD: 404, SEARCH_DOWN: 503}


@get("/radio/{tid:int}")
@inject
async def radio(
    tid: FromPath[int],
    request: Request[User, str, Any],
    service: FromDishka[Radio],
    exclude: FromQuery[str] = "",
    n: Annotated[int, QueryParameter(ge=RADIO_MIN, le=RADIO_MAX)] = RADIO_SIZE,
    mood: FromQuery[str] = "",
) -> RadioView:
    tracks = await service.tracks(request.user.id, tid, excluded_list(exclude), n, mood)
    return {"seed": tid, "tracks": tracks}


@get("/mix/{genre:str}")
@inject
async def mix(
    genre: FromPath[str], request: Request[User, str, Any], mixes: FromDishka[Mixes]
) -> Mix:
    return await mixes.genre_mix(request.user.id, genre)


@get("/moods")
@inject
async def moods(service: FromDishka[Moods]) -> MoodGroups:
    return await service.groups()


@get("/mood/{mood_id:str}")
@inject
async def mood(mood_id: FromPath[str], service: FromDishka[Moods]) -> MoodMix:
    return await service.mix(mood_id)


@get("/mood-search")
@inject
async def mood_search(
    service: FromDishka[Moods],
    q: Annotated[str, QueryParameter(min_length=1, max_length=SEARCH_MAX_CHARS, pattern=r"\S")],
) -> MoodMix:
    return await service.search(q)


router = Router(
    "/api",
    route_handlers=[radio, mix, moods, mood, mood_search],
    exception_handlers=domain_errors(ERRORS),
)
