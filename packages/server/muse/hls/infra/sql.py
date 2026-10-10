import json

from muse.hls.domain import HlsSession
from muse.shared.db import UnitOfWork


class SqlHls:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def create(self, session: HlsSession, now: int) -> bool:
        result = await self.uow.execute(
            "INSERT INTO hls(id, user, ids, at, ended, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                session.id,
                session.user,
                json.dumps(session.ids),
                session.at,
                int(session.ended),
                now,
                now,
            ),
        )
        return result.rowcount == 1

    async def get(self, session_id: str) -> HlsSession | None:
        row = await self.uow.row("SELECT user, ids, at, ended FROM hls WHERE id=?", (session_id,))
        if row is None:
            return None
        ids = json.loads(row["ids"])
        return HlsSession(session_id, row["user"], ids, row["at"], bool(row["ended"]))

    async def save(self, session_id: str, ids: list[int], ended: bool, now: int) -> None:
        await self.uow.execute(
            "UPDATE hls SET ids=?, ended=?, updated_at=? WHERE id=?",
            (json.dumps(ids), int(ended), now, session_id),
        )

    async def purge(self, before: int) -> None:
        await self.uow.execute("DELETE FROM hls WHERE updated_at < ?", (before,))
