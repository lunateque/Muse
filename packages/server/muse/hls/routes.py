from typing import Any

from dishka.integrations.litestar import FromDishka, inject
from litestar import Request, Response, Router, get, post, put
from litestar.params import FromPath

from muse.catalog.domain import BAD_RUN, FILE_GONE, NO_TRACK
from muse.hls.domain import (
    BAD_SESSION_ID,
    NO_SEGMENT,
    NO_SESSION,
    PLAYLIST_NAME,
    PLAYLIST_TYPE,
    SEGMENT_FAILED,
    SEGMENT_NAME,
    SESSION_ID,
    Appended,
    NewSession,
)
from muse.hls.schemas import AppendBody, CreateBody
from muse.hls.service import HlsSessions
from muse.identity.domain import User
from muse.shared.errors import DomainError, domain_errors

ERRORS = {
    NO_TRACK: 404,
    FILE_GONE: 404,
    BAD_RUN: 400,
    NO_SESSION: 404,
    NO_SEGMENT: 404,
    BAD_SESSION_ID: 422,
    SEGMENT_FAILED: 503,
}
MP3 = "audio/mpeg"


@put("/hls/{name:str}")
@inject
async def open_session(
    name: FromPath[str],
    data: CreateBody,
    request: Request[User, str, Any],
    service: FromDishka[HlsSessions],
) -> Response[NewSession]:
    if SESSION_ID.fullmatch(name) is None:
        raise DomainError(BAD_SESSION_ID)
    opened, created = await service.open(request.user.id, name, data.ids, data.at)
    return Response(opened, status_code=201 if created else 200)


@post("/hls/{sid:str}/append", status_code=200)
@inject
async def append(
    sid: FromPath[str],
    data: AppendBody,
    request: Request[User, str, Any],
    service: FromDishka[HlsSessions],
) -> Appended:
    return await service.append(request.user.id, sid, data.ids, end=data.end)


@get("/hls/{name:str}", opt={"skip_auth": True})
@inject
async def playlist(name: FromPath[str], service: FromDishka[HlsSessions]) -> Response[str]:
    if (found := PLAYLIST_NAME.fullmatch(name)) is None:
        raise DomainError(NO_SESSION)
    return Response(await service.playlist(found[1]), media_type=PLAYLIST_TYPE)


@get("/hls/{sid:str}/{song:int}/{name:str}", opt={"skip_auth": True})
@inject
async def segment(
    sid: FromPath[str],
    song: FromPath[int],
    name: FromPath[str],
    request: Request[Any, Any, Any],
    service: FromDishka[HlsSessions],
) -> Response[bytes]:
    if (found := SEGMENT_NAME.fullmatch(name)) is None:
        raise DomainError(NO_SEGMENT)
    known = request.headers.get("if-none-match")
    etag, body = await service.segment(sid, song, int(found[1]), known)
    if body is None:
        return Response(b"", status_code=304, media_type=MP3, headers={"ETag": etag})
    return Response(body, media_type=MP3, headers={"ETag": etag})


router = Router(
    "/api",
    route_handlers=[open_session, append, playlist, segment],
    exception_handlers=domain_errors(ERRORS),
)
