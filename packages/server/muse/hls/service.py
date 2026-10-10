from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence

import anyio
import structlog

from muse.catalog.domain import BAD_RUN, FILE_GONE, NO_TRACK, RunCutError, RunRow
from muse.catalog.service import Catalog
from muse.hls.domain import (
    MAX_SONGS,
    NO_SEGMENT,
    NO_SESSION,
    PLAYLIST_POLL_S,
    PLAYLIST_WAIT_S,
    SEGMENT_FAILED,
    SESSION_TTL_MS,
    SONG_CACHE,
    Appended,
    HlsSession,
    HlsStore,
    NewSession,
    SongAudio,
    SongInfo,
    SongPiece,
)
from muse.hls.render import has_segment, playlist_text, segment_body, segment_ticks, song_pieces
from muse.shared.clock import Clock
from muse.shared.db import UnitOfWork
from muse.shared.errors import DomainError
from muse.shared.mp3 import STREAM_KIND, Joiner

log = structlog.get_logger()

type SongKey = tuple[int, int, int]


def song_infos(rows: Sequence[RunRow]) -> list[SongInfo]:
    return [{"id": row["id"], "dur": row["dur"] or 0} for row in rows]


class SongCache:
    def __init__(self) -> None:
        self.size = SONG_CACHE
        self.songs: OrderedDict[SongKey, SongAudio] = OrderedDict()
        self.building: dict[SongKey, anyio.Lock] = {}

    async def get(self, key: SongKey, build: Callable[[], Awaitable[SongAudio]]) -> SongAudio:
        if (found := self._hit(key)) is not None:
            return found
        lock = self.building.setdefault(key, anyio.Lock())
        try:
            async with lock:
                if (found := self._hit(key)) is not None:
                    return found
                built = await build()
                self.songs[key] = built
                while len(self.songs) > self.size:
                    self.songs.popitem(last=False)
                return built
        finally:
            self.building.pop(key, None)

    def _hit(self, key: SongKey) -> SongAudio | None:
        found = self.songs.get(key)
        if found is not None:
            self.songs.move_to_end(key)
        return found


class HlsSessions:
    def __init__(
        self, store: HlsStore, catalog: Catalog, cache: SongCache, clock: Clock, uow: UnitOfWork
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.cache = cache
        self.clock = clock
        self.uow = uow

    async def open(
        self, user_id: str, session_id: str, ids: Sequence[int], at: float
    ) -> tuple[NewSession, bool]:
        created = False
        session = await self.store.get(session_id)
        if session is None:
            rows = await self.catalog.playable(ids)
            if not rows:
                raise DomainError(NO_TRACK)
            if at >= (rows[0]["dur"] or 0):
                raise DomainError(BAD_RUN)
            if await self.catalog.track_file(rows[0]["path"]) is None:
                raise DomainError(FILE_GONE)
            now = self.clock.now_ms()
            await self.store.purge(now - SESSION_TTL_MS)
            fresh = HlsSession(session_id, user_id, [row["id"] for row in rows], at, ended=False)
            created = await self.store.create(fresh, now)
            await self.uow.commit()
            session = await self.store.get(session_id)
        if session is None or session.user != user_id:
            raise DomainError(NO_SESSION)
        rows = await self.catalog.playable(session.ids)
        url = f"/api/hls/{session_id}.m3u8"
        return {"id": session_id, "url": url, "songs": song_infos(rows)}, created

    async def append(
        self, user_id: str, session_id: str, ids: Sequence[int], *, end: bool
    ) -> Appended:
        session = await self.store.get(session_id)
        if session is None or session.user != user_id:
            raise DomainError(NO_SESSION)
        room = max(0, MAX_SONGS - len(session.ids))
        rows = (await self.catalog.playable(ids))[:room]
        await self.store.save(
            session_id,
            [*session.ids, *(row["id"] for row in rows)],
            session.ended or end,
            self.clock.now_ms(),
        )
        await self.uow.commit()
        return {"songs": song_infos(rows)}

    async def playlist(self, session_id: str) -> str:
        session, _, pieces = await self._timeline(session_id, wait=True)
        return playlist_text(session_id, pieces, ended=session.ended)

    async def segment(self, session_id: str, song: int, index: int) -> bytes:
        _, rows, pieces = await self._timeline(session_id)
        if not has_segment(pieces, song, index):
            raise DomainError(NO_SEGMENT)
        try:
            audio = await self._audio(rows[song], pieces[song])
        except RunCutError as cut:
            log.warning("hls segment", track=rows[song]["id"], song=song, index=index, why=cut.why)
            raise DomainError(SEGMENT_FAILED) from None
        return segment_body(audio, index, segment_ticks(pieces, song, index))

    async def _timeline(
        self, session_id: str, *, wait: bool = False
    ) -> tuple[HlsSession, list[RunRow], list[SongPiece]]:
        session = await self._awaited(session_id) if wait else await self.store.get(session_id)
        if session is None:
            raise DomainError(NO_SESSION)
        rows = await self.catalog.playable(session.ids)
        pieces = song_pieces([float(row["dur"] or 0) for row in rows], session.at)
        return session, rows, pieces

    async def _awaited(self, session_id: str) -> HlsSession | None:
        deadline = anyio.current_time() + PLAYLIST_WAIT_S
        while (session := await self.store.get(session_id)) is None:
            await self.uow.commit()
            if anyio.current_time() >= deadline:
                return None
            await anyio.sleep(PLAYLIST_POLL_S)
        return session

    async def _audio(self, row: RunRow, piece: SongPiece) -> SongAudio:
        async def build() -> SongAudio:
            joiner = Joiner(STREAM_KIND, skip=piece.skip, left=piece.frames)
            return SongAudio.of(await self.catalog.song_audio(row, joiner))

        return await self.cache.get((row["id"], piece.skip, piece.frames), build)
