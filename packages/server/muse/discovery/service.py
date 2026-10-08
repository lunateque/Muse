import hashlib
import random
from collections.abc import Awaitable, Callable
from datetime import date
from functools import partial

import anyio

from muse.catalog.domain import NO_SUCH_GENRE, NO_TRACK, Track, track_of
from muse.catalog.service import Catalog
from muse.discovery.domain import (
    DAY_MS,
    MIX_PLAYED_DAYS,
    MIX_TTL_S,
    NO_MOOD,
    RADIO_RECENT_MS,
    RADIO_SKIP_MS,
    SEARCH_DOWN,
    SEARCH_GROUP,
    GenreTracks,
    ListeningHistory,
    Mix,
    MoodCard,
    MoodGroups,
    MoodMix,
    MoodShelf,
    RadioModel,
    RadioTaste,
    TextEncoder,
    TextUnavailableError,
    TopUp,
    VectorSource,
    build_radio_model,
    draw_mix,
    draw_radio,
    mix_key,
    mix_title,
    mood_shelves,
)
from muse.discovery.draws import (
    draw_mood,
    draw_sound_radio,
    mosaic_rows,
    sound_topup,
    spread_mosaics,
)
from muse.discovery.vectors import Mood, VectorIndex, build_index, grouped
from muse.identity.domain import Users
from muse.shared.cache import Cache
from muse.shared.clock import Clock
from muse.shared.errors import DomainError

MEMO_LIMIT = 1024
SEARCH_ID_CHARS = 10


class FingerprintCache[T]:
    def __init__(self) -> None:
        self._lock = anyio.Lock()
        self._fingerprint: str | None = None
        self._value: T | None = None

    async def get(self, fingerprint: str, build: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if self._value is None or self._fingerprint != fingerprint:
                self._value = await build()
                self._fingerprint = fingerprint
            return self._value


class RadioModelCache(FingerprintCache[RadioModel]):
    pass


class VectorIndexCache(FingerprintCache[VectorIndex]):
    pass


class RadioModels:
    def __init__(self, catalog: Catalog, cache: RadioModelCache) -> None:
        self.catalog = catalog
        self.cache = cache

    async def current(self) -> RadioModel:
        return await self.cache.get(await self.catalog.fingerprint(), self._build)

    async def _build(self) -> RadioModel:
        tracks = await self.catalog.library_tracks()
        return build_radio_model(tracks, await self.catalog.playlist_artists())


class VectorIndexes:
    def __init__(self, catalog: Catalog, source: VectorSource, cache: VectorIndexCache) -> None:
        self.catalog = catalog
        self.source = source
        self.cache = cache

    async def current(self) -> VectorIndex:
        return await self.cache.get(await self.catalog.fingerprint(), self._build)

    async def topup(self) -> TopUp | None:
        index = await self.current()
        if not index.size:
            return None

        def near(ids: list[int], count: int) -> list[int]:
            return sound_topup(index, ids, count)

        return near

    async def _build(self) -> VectorIndex:
        raw = await self.source.load()
        tracks = await self.catalog.library_tracks()
        return await anyio.to_thread.run_sync(build_index, raw, tracks)


class Radio:
    def __init__(
        self,
        models: RadioModels,
        indexes: VectorIndexes,
        history: ListeningHistory,
        users: Users,
        clock: Clock,
    ) -> None:
        self.models = models
        self.indexes = indexes
        self.history = history
        self.users = users
        self.clock = clock

    async def tracks(
        self, user_id: str, seed_id: int, exclude: list[int], count: int, mood: str = ""
    ) -> list[Track]:
        model = await self.models.current()
        seed = model.by_id.get(seed_id)
        if seed is None:
            raise DomainError(NO_TRACK)
        index = await self.indexes.current()
        if mood and mood not in index.by_id:
            raise DomainError(NO_MOOD)
        now = self.clock.now_ms()
        recent = await self.history.played_since(user_id, now - RADIO_RECENT_MS)
        partner = self.users.partner_id(user_id)
        taste = RadioTaste(await self.history.liked(user_id), await self.history.liked(partner))
        skip = set(exclude) | recent | {seed_id}
        if seed_id not in index.row_of:
            return draw_radio(model, seed, skip, taste, count, random.Random())
        skip |= await self.history.skipped_since(user_id, now - RADIO_SKIP_MS)
        scores = index.scores(index.by_id[mood]) if mood else None
        return draw_sound_radio(
            index, seed_id, skip, exclude, taste, scores, count, random.Random()
        )


class Mixes:
    def __init__(
        self,
        catalog: Catalog,
        genre_tracks: GenreTracks,
        history: ListeningHistory,
        cache: Cache,
        clock: Clock,
    ) -> None:
        self.catalog = catalog
        self.genre_tracks = genre_tracks
        self.history = history
        self.cache = cache
        self.clock = clock

    async def mix(self, user_id: str, genre: str) -> Mix:
        day = self.clock.today()
        compose = partial(self._compose, user_id, genre, day)
        return await self.cache.get_or_compute(mix_key(user_id, genre, day), MIX_TTL_S, compose)

    async def genre_mix(self, user_id: str, genre: str) -> Mix:
        if genre not in await self.catalog.genre_counts():
            raise DomainError(NO_SUCH_GENRE)
        return await self.mix(user_id, genre)

    async def _compose(self, user_id: str, genre: str, day: date) -> Mix:
        candidates = await self.genre_tracks.by_id(genre)
        liked = await self.history.liked(user_id)
        since = self.clock.now_ms() - MIX_PLAYED_DAYS * DAY_MS
        played = await self.history.counted_since(user_id, since)
        tracks = draw_mix(candidates, liked, played, random.Random(f"{user_id}:{genre}:{day}"))
        return {
            "genre": genre,
            "title": mix_title(genre),
            "tracks": tracks,
            "albums": await self.catalog.cover_ids(tracks),
        }


class MoodMemo:
    def __init__(self) -> None:
        self._index: VectorIndex | None = None
        self._day: date | None = None
        self._mixes: dict[str, MoodMix] = {}
        self._albums_index: VectorIndex | None = None
        self._albums: dict[str, list[int]] = {}

    def albums(self, index: VectorIndex) -> dict[str, list[int]]:
        if self._albums_index is not index:
            self._albums_index, self._albums = index, {}
        return self._albums

    def _fresh(self, index: VectorIndex, day: date) -> None:
        if self._index is not index or self._day != day or len(self._mixes) >= MEMO_LIMIT:
            self._index, self._day, self._mixes = index, day, {}

    def get(self, index: VectorIndex, day: date, key: str) -> MoodMix | None:
        self._fresh(index, day)
        return self._mixes.get(key)

    def put(self, index: VectorIndex, day: date, key: str, mix: MoodMix) -> None:
        self._fresh(index, day)
        self._mixes[key] = mix


def search_id(normalised: str) -> str:
    return (
        "q:"
        + hashlib.sha1(normalised.encode(), usedforsecurity=False).hexdigest()[:SEARCH_ID_CHARS]
    )


class Moods:
    def __init__(
        self,
        indexes: VectorIndexes,
        catalog: Catalog,
        encoder: TextEncoder,
        memo: MoodMemo,
        clock: Clock,
    ) -> None:
        self.indexes = indexes
        self.catalog = catalog
        self.encoder = encoder
        self.memo = memo
        self.clock = clock

    async def groups(self) -> MoodGroups:
        index = await self.indexes.current()
        return {
            "groups": [
                {"title": title, "moods": await self._cards(index, moods)}
                for title, moods in grouped(index.moods)
            ]
        }

    async def shelves(self) -> list[MoodShelf]:
        index = await self.indexes.current()
        day = self.clock.today()
        return [
            {"title": title, "moods": await self._cards(index, moods)}
            for title, moods in mood_shelves(grouped(index.moods), day)
        ]

    async def mix(self, mood_id: str) -> MoodMix:
        index = await self.indexes.current()
        mood = index.by_id.get(mood_id)
        if mood is None:
            raise DomainError(NO_MOOD)
        return await self._mood_mix(index, mood, self.clock.today())

    async def search(self, text: str) -> MoodMix:
        index = await self.indexes.current()
        if not index.size:
            raise DomainError(SEARCH_DOWN)
        day = self.clock.today()
        title = " ".join(text.split())
        normalised = title.casefold()
        mix_id = search_id(normalised)
        if cached := self.memo.get(index, day, mix_id):
            return cached
        try:
            scores = index.query_scores(await self.encoder.embed(title))
        except TextUnavailableError as error:
            raise DomainError(SEARCH_DOWN) from error
        if scores is None:
            raise DomainError(SEARCH_DOWN)
        rows = draw_mood(index, scores, None, random.Random(f"{normalised}:{day}"))
        mix = await self._compose(index, mix_id, title, SEARCH_GROUP, rows)
        self.memo.put(index, day, mix_id, mix)
        return mix

    async def _cards(self, index: VectorIndex, moods: list[Mood]) -> list[MoodCard]:
        mosaics = await self._mosaics(index)
        return [{"id": mood.id, "title": mood.title, "albums": mosaics[mood.id]} for mood in moods]

    async def _mosaics(self, index: VectorIndex) -> dict[str, list[int]]:
        known = self.memo.albums(index)
        if not known:
            ranked = []
            for mood in index.moods:
                rows = mosaic_rows(index, index.scores(mood), mood.lang)
                tracks = [index.tracks[row] for row in rows]
                ranked.append(await self.catalog.cover_ids(tracks, limit=len(tracks)))
            known.update(
                {
                    mood.id: albums
                    for mood, albums in zip(index.moods, spread_mosaics(ranked), strict=True)
                }
            )
        return known

    async def _mood_mix(self, index: VectorIndex, mood: Mood, day: date) -> MoodMix:
        if cached := self.memo.get(index, day, mood.id):
            return cached
        rnd = random.Random(f"{mood.id}:{day}")
        rows = draw_mood(index, index.scores(mood), mood.lang, rnd)
        mix = await self._compose(index, mood.id, mood.title, mood.group, rows)
        self.memo.put(index, day, mood.id, mix)
        return mix

    async def _compose(
        self, index: VectorIndex, mix_id: str, title: str, group: str, rows: list[int]
    ) -> MoodMix:
        tracks = [track_of(index.tracks[row]) for row in rows]
        albums = await self.catalog.cover_ids(tracks)
        return {"id": mix_id, "title": title, "group": group, "tracks": tracks, "albums": albums}
