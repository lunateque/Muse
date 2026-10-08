import re
from dataclasses import dataclass, field

import numpy as np

from muse.catalog.domain import LibraryTrack
from muse.discovery.domain import RawVectors, song_key

type Matrix = np.ndarray

CYRILLIC = re.compile("[\u0400-\u04ff]")
RU_GENRE = "RU"
SPACE_KEYS = ("dim", "mean", "lang_axis", "version")
EPSILON = 1e-12
CHUNK = 4096


@dataclass(frozen=True, slots=True)
class Mood:
    id: str
    title: str
    group: str
    order: int
    lang: str | None
    row: int


@dataclass(frozen=True, slots=True, eq=False)
class VectorIndex:
    version: str
    tracks: list[LibraryTrack]
    artist_of: dict[int, str]
    song_of: dict[int, str]
    songs: list[str]
    row_of: dict[int, int]
    russian: Matrix
    sound: Matrix
    mood_space: Matrix
    mean: Matrix
    axis: Matrix
    moods: list[Mood]
    mood_scores: Matrix
    by_id: dict[str, Mood] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.tracks)

    def scores(self, mood: Mood) -> Matrix:
        return self.mood_scores[mood.row]

    def query_scores(self, vector: list[float]) -> Matrix | None:
        raw = np.asarray(vector, dtype=np.float32)
        if raw.shape != self.mean.shape:
            return None
        return self.mood_space @ mood_query(raw, self.mean, self.axis)


def unit(values: Matrix) -> Matrix:
    norms = np.linalg.norm(values, axis=-1, keepdims=True)
    return values / np.maximum(norms, EPSILON)


def without_axis(values: Matrix, axis: Matrix) -> Matrix:
    return unit(values - (values @ axis)[..., None] * axis)


def mood_query(raw: Matrix, mean: Matrix, axis: Matrix) -> Matrix:
    return without_axis(unit(raw - mean), axis)


def is_russian(track: LibraryTrack) -> bool:
    return bool(
        CYRILLIC.search(track["artist"])
        or CYRILLIC.search(track["title"])
        or (track["genre"] or "").startswith(RU_GENRE)
    )


def song_keys(tracks: list[LibraryTrack]) -> dict[int, str]:
    return {track["id"]: song_key(track["artist"], track["title"]) for track in tracks}


def empty_index(tracks: list[LibraryTrack]) -> VectorIndex:
    none = np.zeros((0, 0), dtype=np.float32)
    return VectorIndex(
        version="",
        tracks=[],
        artist_of={track["id"]: track["artist"] for track in tracks},
        song_of=song_keys(tracks),
        songs=[],
        row_of={},
        russian=np.zeros(0, dtype=bool),
        sound=none,
        mood_space=none,
        mean=np.zeros(0, dtype=np.float32),
        axis=np.zeros(0, dtype=np.float32),
        moods=[],
        mood_scores=none,
    )


def decode(blobs: list[bytes], dim: int) -> Matrix:
    if not blobs:
        return np.zeros((0, dim), dtype=np.float32)
    flat = np.frombuffer(b"".join(blobs), dtype="<f2")
    return flat.reshape(len(blobs), dim).astype(np.float32)


def sound_space(blobs: list[bytes], dim: int, mean: Matrix) -> Matrix:
    out = np.empty((len(blobs), dim), dtype=np.float32)
    for start in range(0, len(blobs), CHUNK):
        out[start : start + CHUNK] = unit(decode(blobs[start : start + CHUNK], dim) - mean)
    return out


def mood_space_of(sound: Matrix, axis: Matrix) -> Matrix:
    out = np.empty_like(sound)
    for start in range(0, len(sound), CHUNK):
        out[start : start + CHUNK] = without_axis(sound[start : start + CHUNK], axis)
    return out


def space_values(raw: RawVectors) -> tuple[int, Matrix, Matrix, str] | None:
    if any(key not in raw.space for key in SPACE_KEYS):
        return None
    dim = int(raw.space["dim"].decode())
    mean = np.frombuffer(raw.space["mean"], dtype="<f4")
    axis = np.frombuffer(raw.space["lang_axis"], dtype="<f4")
    if dim <= 0 or mean.shape != (dim,) or axis.shape != (dim,):
        return None
    return dim, mean.astype(np.float32), axis.astype(np.float32), raw.space["version"].decode()


def build_index(raw: RawVectors, tracks: list[LibraryTrack]) -> VectorIndex:
    found = space_values(raw)
    if found is None:
        return empty_index(tracks)
    dim, mean, axis, version = found
    by_id = {track["id"]: track for track in tracks}
    rows = [
        (track_id, blob)
        for track_id, blob in zip(raw.track_ids, raw.track_vecs, strict=True)
        if track_id in by_id and len(blob) == dim * 2
    ]
    if not rows:
        return empty_index(tracks)
    held = [by_id[track_id] for track_id, _ in rows]
    sound = sound_space([blob for _, blob in rows], dim, mean)
    mood_space = mood_space_of(sound, axis)
    moods = [mood for mood in raw.moods if len(mood["vec"]) == dim * 2]
    moods.sort(key=lambda mood: (mood["ord"], mood["id"]))
    queries = mood_query(decode([mood["vec"] for mood in moods], dim), mean, axis)
    listed = [
        Mood(mood["id"], mood["title"], mood["grp"], mood["ord"], mood["lang"], number)
        for number, mood in enumerate(moods)
    ]
    return VectorIndex(
        version=version,
        tracks=held,
        artist_of={track["id"]: track["artist"] for track in tracks},
        song_of=song_keys(tracks),
        songs=[song_key(track["artist"], track["title"]) for track in held],
        row_of={track["id"]: row for row, track in enumerate(held)},
        russian=np.array([is_russian(track) for track in held], dtype=bool),
        sound=sound,
        mood_space=mood_space,
        mean=mean,
        axis=axis,
        moods=listed,
        mood_scores=queries @ mood_space.T,
        by_id={mood.id: mood for mood in listed},
    )


def grouped(moods: list[Mood]) -> list[tuple[str, list[Mood]]]:
    groups: dict[str, list[Mood]] = {}
    for mood in moods:
        groups.setdefault(mood.group, []).append(mood)
    return list(groups.items())
