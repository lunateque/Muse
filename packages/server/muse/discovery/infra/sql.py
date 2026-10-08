from typing import cast

from muse.activity.domain import COUNTED, PLAYS_LIB
from muse.catalog.domain import PLAYABLE, TRACK_COLUMNS, Track
from muse.discovery.domain import RADIO_SKIPPED_LISTEN_MS, MoodRow, RawVectors
from muse.shared.db import CatalogDb, UnitOfWork

PLAYED_SINCE = "SELECT track_id FROM plays WHERE user=? AND started_at>=?"
COUNTED_SINCE = (
    f"SELECT DISTINCT p.track_id {PLAYS_LIB}WHERE p.user=? AND p.started_at>=? AND {COUNTED}"
)
LIKED = "SELECT track_id FROM likes WHERE user=?"
SKIPPED_SINCE = (
    "SELECT DISTINCT track_id FROM plays WHERE user=? AND started_at>=? AND skipped=1 "
    "AND listened_ms<?"
)
GENRE_TRACKS = f"SELECT {TRACK_COLUMNS} {PLAYABLE}AND al.genre=? ORDER BY t.id"
VECTOR_TABLES = ("track_vectors", "vector_space", "moods")
HAVE_TABLES = (
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
    f"AND name IN ({','.join('?' * len(VECTOR_TABLES))})"
)
TRACK_VECTORS = (
    "SELECT v.track_id, v.vec FROM track_vectors v JOIN tracks t ON t.id=v.track_id "
    "WHERE t.status='ok'"
)
SPACE = "SELECT key, value FROM vector_space"
MOODS = "SELECT id, title, grp, ord, prompt, lang, vec FROM moods ORDER BY ord, id"


class SqlListeningHistory:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def _ids(self, sql: str, params: tuple[object, ...]) -> set[int]:
        return {row["track_id"] for row in await self.uow.rows(sql, params)}

    async def played_since(self, user_id: str, since_ms: int) -> set[int]:
        return await self._ids(PLAYED_SINCE, (user_id, since_ms))

    async def counted_since(self, user_id: str, since_ms: int) -> set[int]:
        return await self._ids(COUNTED_SINCE, (user_id, since_ms))

    async def liked(self, user_id: str) -> set[int]:
        return await self._ids(LIKED, (user_id,))

    async def skipped_since(self, user_id: str, since_ms: int) -> set[int]:
        return await self._ids(SKIPPED_SINCE, (user_id, since_ms, RADIO_SKIPPED_LISTEN_MS))


class SqlGenreTracks:
    def __init__(self, db: CatalogDb) -> None:
        self.db = db

    async def by_id(self, genre: str) -> list[Track]:
        return cast("list[Track]", await self.db.rows(GENRE_TRACKS, (genre,)))


def as_bytes(value: str | bytes) -> bytes:
    return value.encode() if isinstance(value, str) else value


class SqlVectors:
    def __init__(self, db: CatalogDb) -> None:
        self.db = db

    async def load(self) -> RawVectors:
        if await self.db.value(HAVE_TABLES, VECTOR_TABLES) != len(VECTOR_TABLES):
            return RawVectors([], [], {}, [])
        rows = await self.db.rows(TRACK_VECTORS)
        space = {row["key"]: as_bytes(row["value"]) for row in await self.db.rows(SPACE)}
        moods = [
            cast("MoodRow", {**row, "vec": as_bytes(row["vec"])})
            for row in await self.db.rows(MOODS)
        ]
        return RawVectors(
            [row["track_id"] for row in rows], [as_bytes(row["vec"]) for row in rows], space, moods
        )
