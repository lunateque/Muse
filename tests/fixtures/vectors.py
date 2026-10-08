import re
import sqlite3

import numpy as np

DIM = 16
SEED = 4242
NOISE = 0.3
RUSSIAN_SHIFT = 0.8
MOOD_NOISE = 0.1
VERSION = "fixture-1"
MODEL = "fixture-gemma"
CYRILLIC = re.compile("[\u0400-\u04ff]")
MOODS = (
    ("drive", "Рэп в дорогу", "Движ", "Rap", None),
    ("gym", "Качалка", "Движ", "Rap", "foreign"),
    ("dawn", "Рассвет", "Утро", "Indie", None),
    ("slow", "Медленно", "Утро", "Other", None),
    ("rain", "Дождь", "Грусть", "Indie", None),
    ("tears", "Слёзы", "Грусть", "RU Rap", "ru"),
    ("road", "Шоссе", "Дорога", "Rap", None),
    ("dreams", "Сны", "Дорога", "Other", None),
)

type Sound = tuple[int, str, str, str]


def unit(values: np.ndarray) -> np.ndarray:
    return values / np.linalg.norm(values, axis=-1, keepdims=True)


def russian(genre: str, artist: str, title: str) -> bool:
    return bool(CYRILLIC.search(artist) or CYRILLIC.search(title) or genre.startswith("RU"))


def write_vectors(connection: sqlite3.Connection, tracks: list[Sound], genres: list[str]) -> None:
    rng = np.random.default_rng(SEED)
    centres = {genre: unit(rng.normal(size=DIM)) for genre in genres}
    shift = np.eye(DIM)[0] * RUSSIAN_SHIFT
    is_ru = np.array([russian(genre, artist, title) for _, genre, artist, title in tracks])
    raw = np.array([centres[genre] for _, genre, _, _ in tracks])
    raw = raw + NOISE * rng.normal(size=raw.shape) + is_ru[:, None] * shift
    mean = raw.mean(axis=0)
    axis = unit(raw[is_ru].mean(axis=0) - raw[~is_ru].mean(axis=0))
    connection.executemany(
        "INSERT INTO track_vectors(track_id, vec) VALUES (?, ?)",
        [(track[0], row.astype("<f2").tobytes()) for track, row in zip(tracks, raw, strict=True)],
    )
    space = {
        "model": MODEL,
        "dim": str(DIM),
        "mean": mean.astype("<f4").tobytes(),
        "lang_axis": axis.astype("<f4").tobytes(),
        "version": VERSION,
    }
    connection.executemany(
        "INSERT INTO vector_space(key, value) VALUES (?, ?)", list(space.items())
    )
    for order, (mood_id, title, group, genre, lang) in enumerate(MOODS, start=1):
        vec = centres[genre] + MOOD_NOISE * rng.normal(size=DIM)
        connection.execute(
            "INSERT INTO moods(id, title, grp, ord, prompt, lang, vec) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mood_id,
                title,
                group,
                order,
                f"{genre} sound",
                lang,
                unit(vec).astype("<f2").tobytes(),
            ),
        )


def drop_vectors(connection: sqlite3.Connection) -> None:
    for table in ("track_vectors", "vector_space", "moods"):
        connection.execute(f"DROP TABLE {table}")
