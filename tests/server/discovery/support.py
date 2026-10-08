import numpy as np

from muse.catalog.domain import LibraryTrack
from muse.discovery.domain import MoodRow, RawVectors
from muse.discovery.vectors import VectorIndex, build_index

DIM = 8
RU_AXIS = 7
NOISE = 0.12
RUSSIAN_PUSH = 0.6
SPACE_VERSION = "test-1"
REISSUES = ("", " (Remastered 2011)", " - Live", " [Deluxe]")

type Group = tuple[tuple[int, ...], int, bool, int]


def track(track_id: int, artist: str, title: str, genre: str | None = "Rap") -> LibraryTrack:
    return {
        "id": track_id,
        "num": 1,
        "title": title,
        "dur": 200,
        "album_id": track_id,
        "album": f"Album {track_id}",
        "artist": artist,
        "genre": genre,
    }


def blob(values: np.ndarray) -> bytes:
    return values.astype("<f2").tobytes()


def space(mean: np.ndarray | None = None) -> dict[str, bytes]:
    axis = np.eye(DIM)[RU_AXIS]
    return {
        "model": b"test",
        "dim": str(DIM).encode(),
        "mean": (np.zeros(DIM) if mean is None else mean).astype("<f4").tobytes(),
        "lang_axis": axis.astype("<f4").tobytes(),
        "version": SPACE_VERSION.encode(),
    }


def mood_row(mood_id: str, group: str, order: int, cluster: int, lang: str | None) -> MoodRow:
    return {
        "id": mood_id,
        "title": mood_id.title(),
        "grp": group,
        "ord": order,
        "prompt": "sound",
        "lang": lang,
        "vec": blob(np.eye(DIM)[cluster]),
    }


def clustered(
    groups: list[Group], moods: list[MoodRow] | None = None, noise: float = NOISE, copies: int = 1
) -> tuple[VectorIndex, list[LibraryTrack]]:
    rng = np.random.default_rng(5)
    tracks: list[LibraryTrack] = []
    blobs: list[bytes] = []
    for centre, count, russian, artists in groups:
        name = "".join(str(part) for part in centre)
        for number in range(count):
            track_id = len(tracks) + 1
            song, copy = divmod(number, copies)
            title = f"Песня {name}{song}" if russian else f"Song {name}{song}"
            title += REISSUES[copy % len(REISSUES)]
            tracks.append(
                track(track_id, f"Artist {name}{'ru' if russian else ''}-{song % artists}", title)
            )
            vector = np.eye(DIM)[list(centre)].mean(axis=0) + noise * rng.normal(size=DIM)
            vector[RU_AXIS] = RUSSIAN_PUSH * russian + noise * rng.normal()
            blobs.append(blob(vector))
    raw = RawVectors([item["id"] for item in tracks], blobs, space(), moods or [])
    return build_index(raw, tracks), tracks
