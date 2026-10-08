import math
import random
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import islice
from typing import Protocol, TypedDict

from muse.catalog.domain import LibraryTrack, Track, track_of

type Links = dict[str, dict[str, float]]

DAY_MS = 86400 * 1000
RADIO_SIZE = 25
RADIO_MIN = 5
RADIO_MAX = 50
RADIO_RECENT_MS = 6 * 3600 * 1000
RADIO_EXCLUDE_LIMIT = 300
SEED_ARTIST_CAP = 3
ARTIST_CAP = 2
UNGROUPED_GENRES = (None, "Other")
MIX_SIZE = 200
RADIO_SKIP_MS = 30 * DAY_MS
RADIO_SKIPPED_LISTEN_MS = 30000
NO_MOOD = "no mood"
SEARCH_DOWN = "mood search unavailable"
SEARCH_MAX_CHARS = 120
SEARCH_GROUP = "Поиск"
SHELF_GROUPS = 3
SHELF_MOODS = 8
MIX_PLAYED_DAYS = 90
MIX_TTL_S = 86400

type TopUp = Callable[[list[int], int], list[int]]

BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
VERSION_TAIL = re.compile(
    r"\s+[-\u2013\u2014]\s+[^-\u2013\u2014]*\b(live|remaster\w*|version|mix|edit|mono|stereo|acoustic|demo"
    r"|radio|deluxe|bonus|\d{4})\b.*$"
)
FEATURING = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s.*$")


class Mix(TypedDict):
    genre: str
    title: str
    tracks: list[Track]
    albums: list[int]


class MoodCard(TypedDict):
    id: str
    title: str
    albums: list[int]


class MoodShelf(TypedDict):
    title: str
    moods: list[MoodCard]


class MoodGroups(TypedDict):
    groups: list[MoodShelf]


class MoodMix(TypedDict):
    id: str
    title: str
    group: str
    tracks: list[Track]
    albums: list[int]


class MoodRow(TypedDict):
    id: str
    title: str
    grp: str
    ord: int
    prompt: str
    lang: str | None
    vec: bytes


@dataclass(frozen=True, slots=True)
class RawVectors:
    track_ids: list[int]
    track_vecs: list[bytes]
    space: dict[str, bytes]
    moods: list[MoodRow]


class TextUnavailableError(Exception):
    pass


class RadioView(TypedDict):
    seed: int
    tracks: list[Track]


@dataclass(frozen=True, slots=True)
class RadioModel:
    tracks: list[LibraryTrack]
    by_id: dict[int, LibraryTrack]
    links: Links


@dataclass(frozen=True, slots=True)
class RadioTaste:
    mine: set[int]
    partner: set[int]


def build_radio_model(tracks: list[LibraryTrack], playlists: Iterable[set[str]]) -> RadioModel:
    links: Links = {}
    for artists in playlists:
        weight = 1 / math.sqrt(len(artists) or 1)
        for artist in artists:
            near = links.setdefault(artist, {})
            for other in artists:
                if other != artist:
                    near[other] = near.get(other, 0) + weight
    return RadioModel(tracks, {track["id"]: track for track in tracks}, links)


def song_key(artist: str, title: str) -> str:
    name = BRACKETS.sub(" ", title.casefold())
    name = FEATURING.sub("", " ".join(name.split()))
    name = VERSION_TAIL.sub("", name)
    return f"{artist.casefold().strip()}|{' '.join(name.split()).strip(' -')}"


def unique_songs[T: Track](tracks: Iterable[T], seen: set[str]) -> Iterator[T]:
    for track in tracks:
        key = song_key(track["artist"], track["title"])
        if key not in seen:
            seen.add(key)
            yield track


def excluded_list(exclude: str) -> list[int]:
    items = exclude.split(",")[:RADIO_EXCLUDE_LIMIT]
    return [int(item) for item in items if item.strip().isdigit()]


def excluded_ids(exclude: str) -> set[int]:
    return set(excluded_list(exclude))


def draw_radio(
    model: RadioModel,
    seed: LibraryTrack,
    skip: set[int],
    taste: RadioTaste,
    count: int,
    rnd: random.Random,
) -> list[Track]:
    links = model.links.get(seed["artist"], {})
    top = max(links.values(), default=1)
    genre = seed["genre"] if seed["genre"] not in UNGROUPED_GENRES else None
    keyed = []
    for track in model.tracks:
        if track["id"] in skip:
            continue
        artist = track["artist"]
        weight = 2.0 if artist == seed["artist"] else 2.5 * links.get(artist, 0) / top
        weight += 0.6 if genre and track["genre"] == genre else 0
        weight += 0.8 * (track["id"] in taste.mine) + 0.5 * (track["id"] in taste.partner) + 0.02
        keyed.append((rnd.random() ** (1 / weight), track))
    keyed.sort(key=lambda pair: -pair[0])
    songs = unique_songs((track for _, track in keyed), {song_key(seed["artist"], seed["title"])})
    return spaced(capped(songs, seed["artist"], count), seed["artist"])


def capped(tracks: Iterable[LibraryTrack], seed_artist: str, count: int) -> list[LibraryTrack]:
    per: dict[str, int] = {}
    picked: list[LibraryTrack] = []
    for track in tracks:
        artist = track["artist"]
        if per.get(artist, 0) < (SEED_ARTIST_CAP if artist == seed_artist else ARTIST_CAP):
            per[artist] = per.get(artist, 0) + 1
            picked.append(track)
            if len(picked) == count:
                break
    return picked


def first_other_artist(tracks: Sequence[Track], artist: str) -> int:
    return next((index for index, track in enumerate(tracks) if track["artist"] != artist), 0)


def spaced(picked: Sequence[LibraryTrack], seed_artist: str) -> list[Track]:
    remaining = list(picked)
    out: list[Track] = []
    last = seed_artist
    while remaining:
        track = remaining.pop(first_other_artist(remaining, last))
        last = track["artist"]
        out.append(track_of(track))
    return out


def mix_weight(track_id: int, liked: set[int], played: set[int]) -> int:
    return 4 if track_id in liked else 2 if track_id in played else 1


def draw_mix(
    tracks: Sequence[Track], liked: set[int], played: set[int], rnd: random.Random
) -> list[Track]:
    keyed = [
        (rnd.random() ** (1 / mix_weight(track["id"], liked, played)), track) for track in tracks
    ]
    keyed.sort(key=lambda pair: -pair[0])
    unique = unique_songs((track for _, track in keyed), set())
    return list(islice(unique, MIX_SIZE))


def mix_title(genre: str) -> str:
    return f"Микс · {genre}"


def mix_key(user_id: str, genre: str, day: date) -> str:
    return f"user:{user_id}:mix:{genre}:{day.isoformat()}"


def mood_shelves[T](
    groups: Sequence[tuple[str, Sequence[T]]], day: date
) -> list[tuple[str, list[T]]]:
    if not groups:
        return []
    number = day.toordinal()
    first = number * SHELF_GROUPS % len(groups)
    shelves: list[tuple[str, list[T]]] = []
    for step in range(min(SHELF_GROUPS, len(groups))):
        title, moods = groups[(first + step) % len(groups)]
        turn = number % len(moods)
        shelves.append((title, [*moods[turn:], *moods[:turn]][:SHELF_MOODS]))
    return shelves


class ListeningHistory(Protocol):
    async def played_since(self, user_id: str, since_ms: int) -> set[int]: ...

    async def counted_since(self, user_id: str, since_ms: int) -> set[int]: ...

    async def liked(self, user_id: str) -> set[int]: ...

    async def skipped_since(self, user_id: str, since_ms: int) -> set[int]: ...


class GenreTracks(Protocol):
    async def by_id(self, genre: str) -> list[Track]: ...


class VectorSource(Protocol):
    async def load(self) -> RawVectors: ...


class TextEncoder(Protocol):
    async def embed(self, text: str) -> list[float]: ...
