from muse.shared.db import Row, UnitOfWork
from muse.together.domain import Letter, OursMark, Pinned, Secret


def letter(row: Row) -> Letter:
    return Letter(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"],
        track_id=row["track_id"],
        text=row["text"],
        created_at=row["created_at"],
        read_at=row["read_at"],
    )


class SqlTogether:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def accrue(self, day: str, seconds: float) -> float:
        await self.uow.execute(
            "INSERT INTO together(day, seconds) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds",
            (day, seconds),
        )
        return await self.uow.value("SELECT seconds FROM together WHERE day=?", (day,))


def secret(row: Row) -> Secret:
    return Secret(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"],
        track_id=row["track_id"],
        text=row["text"],
        created_at=row["created_at"],
        found_at=row["found_at"],
    )


class SqlSecrets:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def hide(self, sender: str, recipient: str, track_id: int, text: str, now: int) -> int:
        await self.uow.execute(
            "DELETE FROM secrets WHERE sender=? AND track_id=? AND found_at IS NULL",
            (sender, track_id),
        )
        result = await self.uow.execute(
            "INSERT INTO secrets(sender, recipient, track_id, text, created_at) VALUES (?,?,?,?,?)",
            (sender, recipient, track_id, text, now),
        )
        return result.lastrowid

    async def waiting_for(self, recipient: str, track_id: int) -> Secret | None:
        row = await self.uow.row(
            "SELECT * FROM secrets WHERE recipient=? AND track_id=? AND found_at IS NULL",
            (recipient, track_id),
        )
        return secret(row) if row else None

    async def mark_found(self, secret_id: int, now: int) -> None:
        await self.uow.execute(
            "UPDATE secrets SET found_at=? WHERE id=? AND found_at IS NULL", (now, secret_id)
        )

    async def hidden_by(self, sender: str) -> list[Secret]:
        rows = await self.uow.rows(
            "SELECT * FROM secrets WHERE sender=? ORDER BY created_at DESC, id DESC LIMIT 100",
            (sender,),
        )
        return [secret(row) for row in rows]

    async def found_by(self, recipient: str) -> list[Secret]:
        rows = await self.uow.rows(
            "SELECT * FROM secrets WHERE recipient=? AND found_at IS NOT NULL "
            "ORDER BY found_at DESC, id DESC LIMIT 100",
            (recipient,),
        )
        return [secret(row) for row in rows]

    async def drop(self, secret_id: int) -> None:
        await self.uow.execute("DELETE FROM secrets WHERE id=?", (secret_id,))


class SqlPins:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def set(self, for_user: str, day: str, pin: Pinned) -> None:
        await self.uow.execute(
            "INSERT INTO pins(for_user, day, track_id, text, set_by, set_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(for_user, day) DO UPDATE SET track_id=excluded.track_id, "
            "text=excluded.text, set_by=excluded.set_by, set_at=excluded.set_at",
            (for_user, day, pin.track_id, pin.text, pin.set_by, pin.set_at),
        )

    async def remove(self, for_user: str, day: str, set_by: str) -> None:
        await self.uow.execute(
            "DELETE FROM pins WHERE for_user=? AND day=? AND set_by=?", (for_user, day, set_by)
        )

    async def of_day(self, day: str) -> list[Pinned]:
        rows = await self.uow.rows(
            "SELECT for_user, day, track_id, text, set_by, set_at FROM pins WHERE day=?", (day,)
        )
        return [Pinned(**row) for row in rows]


class SqlLetters:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def send(self, sender: str, recipient: str, track_id: int, text: str, now: int) -> int:
        result = await self.uow.execute(
            "INSERT INTO letters(sender, recipient, track_id, text, created_at) VALUES (?,?,?,?,?)",
            (sender, recipient, track_id, text, now),
        )
        return result.lastrowid

    async def received(self, user_id: str) -> list[Letter]:
        rows = await self.uow.rows(
            "SELECT * FROM letters WHERE recipient=? ORDER BY created_at DESC, id DESC LIMIT 100",
            (user_id,),
        )
        return [letter(row) for row in rows]

    async def sent(self, user_id: str) -> list[Letter]:
        rows = await self.uow.rows(
            "SELECT * FROM letters WHERE sender=? ORDER BY created_at DESC, id DESC LIMIT 100",
            (user_id,),
        )
        return [letter(row) for row in rows]

    async def addressed_to(self, letter_id: int, user_id: str) -> bool:
        found = await self.uow.value(
            "SELECT 1 FROM letters WHERE id=? AND recipient=?", (letter_id, user_id)
        )
        return found is not None

    async def mark_read(self, letter_id: int, now: int) -> None:
        await self.uow.execute(
            "UPDATE letters SET read_at=? WHERE id=? AND read_at IS NULL", (now, letter_id)
        )

    async def unread(self, user_id: str) -> int:
        return await self.uow.value(
            "SELECT COUNT(*) FROM letters WHERE recipient=? AND read_at IS NULL", (user_id,)
        )


class SqlOurs:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def marks(self) -> list[OursMark]:
        rows = await self.uow.rows(
            "SELECT track_id, added_by, added_at FROM ours ORDER BY added_at DESC, rowid DESC"
        )
        return [OursMark(row["track_id"], row["added_by"], row["added_at"]) for row in rows]

    async def add(self, track_id: int, user_id: str, now: int) -> bool:
        result = await self.uow.execute(
            "INSERT OR IGNORE INTO ours VALUES (?,?,?)", (track_id, user_id, now)
        )
        return result.rowcount == 1

    async def remove(self, track_id: int) -> None:
        await self.uow.execute("DELETE FROM ours WHERE track_id=?", (track_id,))
