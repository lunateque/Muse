import random

import anyio

from muse.catalog.domain import Track
from muse.catalog.service import Catalog
from muse.identity.domain import Users
from muse.shared.clock import Clock
from muse.shared.db import UnitOfWork
from muse.shared.errors import DomainError
from muse.shared.events import EventBus
from muse.together.dates import (
    DatesSource,
    DatesView,
    parse_dates,
    todays,
    together_days,
    together_since,
)
from muse.together.domain import (
    FOLLOW_POLL_S,
    NO_LETTER,
    NO_WAITING,
    PARTNER_FOLLOWS,
    Beat,
    FollowView,
    LetterBox,
    LetterList,
    LetterSent,
    LiveBoard,
    LiveView,
    Mirrored,
    NotesSource,
    NotesView,
    OursChanged,
    OursList,
    OursTrack,
    OursView,
    PinBoard,
    Pinned,
    PinSet,
    PinsView,
    Secret,
    SecretBox,
    SecretFound,
    SecretList,
    SecretView,
    TogetherAccrued,
    TogetherLedger,
    letter_text,
    live_partner,
    mirror,
    mirrored_track_id,
    pin_text,
    shared_seconds,
)


class Live:
    def __init__(
        self,
        board: LiveBoard,
        ledger: TogetherLedger,
        letters: LetterBox,
        catalog: Catalog,
        users: Users,
        clock: Clock,
        bus: EventBus,
        uow: UnitOfWork,
    ) -> None:
        self.board = board
        self.ledger = ledger
        self.letters = letters
        self.catalog = catalog
        self.users = users
        self.clock = clock
        self.bus = bus
        self.uow = uow

    async def beat(
        self, user_id: str, track_id: int | None, position: float, playing: bool
    ) -> LiveView:
        if track_id is not None:
            await self.catalog.playable_duration(track_id)
        now = self.clock.now_ms()
        current = Beat(track_id, max(0.0, position), playing, now)
        previous, partner = self.board.report(user_id, self.users.partner_id(user_id), current)
        shared = shared_seconds(previous, current, partner)
        if shared is not None:
            total = await self.ledger.accrue(self.clock.today().isoformat(), shared)
            await self.bus.publish(TogetherAccrued(total - shared, total))
            await self.uow.commit()
        return await self._view(user_id, now)

    async def view(self, user_id: str) -> LiveView:
        return await self._view(user_id, self.clock.now_ms())

    async def _view(self, user_id: str, now: int) -> LiveView:
        beat, following = self.board.partner_state(self.users.partner_id(user_id), now)
        track_id = beat.track_id if beat else None
        partner = None
        if beat is not None and track_id is not None:
            track = (await self.catalog.track_rows([track_id])).get(track_id)
            partner = live_partner(beat, track, now, following) if track else None
        return {
            "partner": partner,
            "together": self.board.together_now(now),
            "unread": await self.letters.unread(user_id),
        }


class Following:
    def __init__(self, board: LiveBoard, catalog: Catalog, users: Users, clock: Clock) -> None:
        self.board = board
        self.catalog = catalog
        self.users = users
        self.clock = clock

    async def follow(self, user_id: str, since: int, wait: float) -> FollowView:
        partner_id = self.users.partner_id(user_id)
        if not self.board.start_following(user_id, partner_id, self.clock.now_ms()):
            raise DomainError(PARTNER_FOLLOWS)
        deadline = anyio.current_time() + wait
        while True:
            beat = self.board.beat_of(partner_id)
            if (beat is not None and beat.at != since) or anyio.current_time() >= deadline:
                return {"partner": await self._mirrored(beat, self.clock.now_ms())}
            await anyio.sleep(FOLLOW_POLL_S)

    def unfollow(self, user_id: str) -> None:
        self.board.stop_following(user_id)

    async def _mirrored(self, beat: Beat | None, now: int) -> Mirrored | None:
        track_id = mirrored_track_id(beat, now)
        if beat is None or track_id is None:
            return None
        track = (await self.catalog.track_rows([track_id])).get(track_id)
        return mirror(beat, track, now) if track else None


class Letters:
    def __init__(
        self,
        letters: LetterBox,
        catalog: Catalog,
        users: Users,
        clock: Clock,
        bus: EventBus,
        uow: UnitOfWork,
    ) -> None:
        self.letters = letters
        self.catalog = catalog
        self.users = users
        self.clock = clock
        self.bus = bus
        self.uow = uow

    async def send(self, user_id: str, track_id: int, text: str) -> dict[str, int]:
        body = letter_text(text)
        await self.catalog.playable_duration(track_id)
        recipient = self.users.partner_id(user_id)
        letter_id = await self.letters.send(user_id, recipient, track_id, body, self.clock.now_ms())
        await self.bus.publish(LetterSent(user_id, recipient, letter_id, track_id, body))
        await self.uow.commit()
        return {"id": letter_id}

    async def listing(self, user_id: str) -> LetterList:
        received = await self.letters.received(user_id)
        sent = await self.letters.sent(user_id)
        tracks = await self.catalog.track_rows([letter.track_id for letter in (*received, *sent)])
        return {
            "received": [letter.view(tracks) for letter in received],
            "sent": [letter.view(tracks) for letter in sent],
        }

    async def mark_read(self, user_id: str, letter_id: int) -> None:
        if not await self.letters.addressed_to(letter_id, user_id):
            raise DomainError(NO_LETTER)
        await self.letters.mark_read(letter_id, self.clock.now_ms())
        await self.uow.commit()


class Ours:
    def __init__(
        self, ours: OursList, catalog: Catalog, clock: Clock, bus: EventBus, uow: UnitOfWork
    ) -> None:
        self.ours = ours
        self.catalog = catalog
        self.clock = clock
        self.bus = bus
        self.uow = uow

    async def listing(self) -> OursView:
        marks = await self.ours.marks()
        rows = await self.catalog.track_rows([mark.track_id for mark in marks])
        tracks: list[OursTrack] = [
            {**rows[mark.track_id], "added_by": mark.added_by, "added_at": mark.added_at}
            for mark in marks
            if mark.track_id in rows
        ]
        return {"tracks": tracks, "albums": await self.catalog.cover_ids(tracks)}

    async def add(self, user_id: str, track_id: int) -> None:
        await self.catalog.playable_duration(track_id)
        added = await self.ours.add(track_id, user_id, self.clock.now_ms())
        await self.bus.publish(OursChanged(user_id, track_id, added_new=added))
        await self.uow.commit()

    async def remove(self, user_id: str, track_id: int) -> None:
        await self.ours.remove(track_id)
        await self.bus.publish(OursChanged(user_id, track_id, added_new=False))
        await self.uow.commit()


class Notes:
    def __init__(self, source: NotesSource, catalog: Catalog, clock: Clock) -> None:
        self.source = source
        self.catalog = catalog
        self.clock = clock

    async def daily(self, user_id: str) -> NotesView:
        pool = await self.source.notes(user_id)
        tracks = await self.catalog.track_rows(
            [note["track_id"] for note in pool if note.get("track_id") is not None]
        )
        enriched = [
            {**note, "track": tracks[note["track_id"]]}
            if note.get("track_id") is not None
            else note
            for note in pool
            if note.get("track_id") is None or note["track_id"] in tracks
        ]
        shuffle = random.Random(f"{user_id}:notes:{self.clock.today()}")
        return {"notes": shuffle.sample(enriched, len(enriched))}


class Secrets:
    def __init__(
        self,
        secrets: SecretBox,
        catalog: Catalog,
        users: Users,
        clock: Clock,
        bus: EventBus,
        uow: UnitOfWork,
    ) -> None:
        self.secrets = secrets
        self.catalog = catalog
        self.users = users
        self.clock = clock
        self.bus = bus
        self.uow = uow

    async def hide(self, user_id: str, track_id: int, text: str) -> dict[str, int]:
        body = letter_text(text)
        await self.catalog.playable_duration(track_id)
        recipient = self.users.partner_id(user_id)
        secret_id = await self.secrets.hide(user_id, recipient, track_id, body, self.clock.now_ms())
        await self.uow.commit()
        return {"id": secret_id}

    async def find(self, user_id: str, track_id: int) -> dict[str, SecretView | None]:
        found = await self.secrets.waiting_for(user_id, track_id)
        if found is None:
            return {"secret": None}
        now = self.clock.now_ms()
        await self.secrets.mark_found(found.id, now)
        await self.bus.publish(SecretFound(found.sender, user_id, found.id, track_id))
        await self.uow.commit()
        return {"secret": {**found.view(await self._tracks([found])), "found_at": now}}

    async def listing(self, user_id: str) -> SecretList:
        hidden = await self.secrets.hidden_by(user_id)
        found = await self.secrets.found_by(user_id)
        tracks = await self._tracks([*hidden, *found])
        return {
            "hidden": [secret.view(tracks) for secret in hidden],
            "found": [secret.view(tracks) for secret in found],
        }

    async def drop(self, user_id: str, secret_id: int) -> None:
        hidden = await self.secrets.hidden_by(user_id)
        waiting = next((secret for secret in hidden if secret.id == secret_id), None)
        if waiting is None or waiting.found_at is not None:
            raise DomainError(NO_WAITING)
        await self.secrets.drop(secret_id)
        await self.uow.commit()

    async def _tracks(self, secrets: list[Secret]) -> dict[int, Track]:
        return await self.catalog.track_rows([secret.track_id for secret in secrets])


class Pins:
    def __init__(
        self,
        pins: PinBoard,
        catalog: Catalog,
        users: Users,
        clock: Clock,
        bus: EventBus,
        uow: UnitOfWork,
    ) -> None:
        self.pins = pins
        self.catalog = catalog
        self.users = users
        self.clock = clock
        self.bus = bus
        self.uow = uow

    async def set(self, user_id: str, track_id: int, text: str) -> None:
        body = pin_text(text)
        await self.catalog.playable_duration(track_id)
        day = self.clock.today().isoformat()
        for_user = self.users.partner_id(user_id)
        pin = Pinned(for_user, day, track_id, body, user_id, self.clock.now_ms())
        await self.pins.set(for_user, day, pin)
        await self.bus.publish(PinSet(for_user, day, track_id, user_id))
        await self.uow.commit()

    async def remove(self, user_id: str) -> None:
        await self.pins.remove(
            self.users.partner_id(user_id), self.clock.today().isoformat(), user_id
        )
        await self.uow.commit()

    async def view(self, user_id: str) -> PinsView:
        partner = self.users.partner_id(user_id)
        day = self.clock.today().isoformat()
        rows = await self.pins.of_day(day)
        for_me = next((pin for pin in rows if pin.for_user == user_id), None)
        from_me = next((pin for pin in rows if pin.for_user == partner), None)
        tracks = await self.catalog.track_rows(
            [pin.track_id for pin in (for_me, from_me) if pin is not None]
        )
        mine = (
            for_me.view(tracks[for_me.track_id]) if for_me and for_me.track_id in tracks else None
        )
        theirs = (
            from_me.view(tracks[from_me.track_id])
            if from_me and from_me.track_id in tracks
            else None
        )
        return {"for_me": mine, "from_me": theirs}


class Dates:
    def __init__(self, source: DatesSource, clock: Clock) -> None:
        self.source = source
        self.clock = clock

    async def view(self) -> DatesView:
        document = await self.source.load()
        today = self.clock.today()
        since = together_since(document)
        return {
            "together_days": together_days(since, today) if since else None,
            "today": todays(parse_dates(document), today),
        }
