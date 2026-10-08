import random
from collections import Counter

import numpy as np

from muse.catalog.domain import Track
from muse.discovery.domain import RadioTaste, capped, spaced, unique_songs
from muse.discovery.vectors import EPSILON, Matrix, VectorIndex, unit

MOOD_SIZE = 200
MOOD_POOL = 2500
MOOD_TEMPERATURE = 0.03
MOOD_ARTIST_CAP = 3
MOOD_SIDE_SHARE = 0.75
MOOD_SIDE_MARGIN = 0.08
MOSAIC_WINDOW = 600
MOSAIC_ALBUMS = 4
MOSAIC_REUSE = 2
SMOOTH_SCORE = 0.5
RADIO_POOL = 400
RADIO_TEMPERATURE = 0.025
RADIO_RECENT = 6
RADIO_SEED_WEIGHT = 0.6
RADIO_SESSION_WEIGHT = 0.4
RADIO_MOOD_WEIGHT = 0.35
RADIO_LIKED_BONUS = 0.02
RADIO_PARTNER_BONUS = 0.01
TOPUP_ARTIST_CAP = 3
RUSSIAN = "ru"
FOREIGN = "foreign"


class Picks:
    def __init__(self, index: VectorIndex, size: int, side_cap: int, artist_cap: int) -> None:
        self.index = index
        self.size = size
        self.side_cap = side_cap
        self.artist_cap = artist_cap
        self.rows: list[int] = []
        self.sides: Counter[bool] = Counter()
        self.artists: Counter[str] = Counter()
        self.songs: set[str] = set()

    @property
    def full(self) -> bool:
        return len(self.rows) >= self.size

    def take(self, candidates: list[int], scores: Matrix, floor: float) -> None:
        for row in candidates:
            artist = self.index.tracks[row]["artist"]
            song = self.index.songs[row]
            side = bool(self.index.russian[row])
            forced = self.sides[not side] >= self.side_cap
            if (
                song in self.songs
                or self.artists[artist] >= self.artist_cap
                or self.sides[side] >= self.side_cap
                or (forced and scores[row] < floor)
            ):
                continue
            self.songs.add(song)
            self.rows.append(row)
            self.artists[artist] += 1
            self.sides[side] += 1
            if self.full:
                break


def sampled_order(scores: Matrix, temperature: float, rnd: random.Random) -> Matrix:
    uniform = np.array([1 - rnd.random() for _ in range(len(scores))])
    noise = -np.log(-np.log(np.clip(uniform, EPSILON, 1 - EPSILON)))
    return np.argsort(-(scores / temperature + noise), kind="stable")


def best_rows(scores: Matrix, rows: Matrix, limit: int) -> Matrix:
    part = scores[rows]
    if len(rows) > limit:
        keep = np.argpartition(-part, limit - 1)[:limit]
        rows, part = rows[keep], part[keep]
    return rows[np.argsort(-part, kind="stable")]


def side_rows(index: VectorIndex, lang: str | None) -> Matrix:
    if lang == RUSSIAN:
        return np.flatnonzero(index.russian)
    if lang == FOREIGN:
        return np.flatnonzero(~index.russian)
    return np.arange(index.size)


def smooth_order(index: VectorIndex, scores: Matrix, picks: list[int]) -> list[int]:
    if len(picks) < 2:
        return list(picks)
    sound = index.sound[picks]
    near = sound @ sound.T
    fit = scores[picks]
    current = int(np.argmax(fit))
    left = np.ones(len(picks), dtype=bool)
    order = []
    while True:
        order.append(current)
        left[current] = False
        if not left.any():
            break
        gain = SMOOTH_SCORE * fit + (1 - SMOOTH_SCORE) * near[current]
        current = int(np.argmax(np.where(left, gain, -np.inf)))
    return [picks[position] for position in order]


def mosaic_rows(index: VectorIndex, scores: Matrix, lang: str | None) -> list[int]:
    return best_rows(scores, side_rows(index, lang), MOSAIC_WINDOW).tolist()


def spread_mosaics(ranked: list[list[int]]) -> list[list[int]]:
    used: Counter[int] = Counter()
    mosaics: list[list[int]] = []
    for albums in ranked:
        chosen = [album for album in albums if used[album] < MOSAIC_REUSE][:MOSAIC_ALBUMS]
        used.update(chosen)
        mosaics.append(chosen)
    return mosaics


def draw_mood(
    index: VectorIndex, scores: Matrix, lang: str | None, rnd: random.Random
) -> list[int]:
    allowed = side_rows(index, lang)
    ranked = best_rows(scores, allowed, MOOD_POOL)
    drawn = ranked[sampled_order(scores[ranked], MOOD_TEMPERATURE, rnd)]
    side_cap = MOOD_SIZE if lang in (RUSSIAN, FOREIGN) else int(MOOD_SIZE * MOOD_SIDE_SHARE)
    picks = Picks(index, MOOD_SIZE, side_cap, MOOD_ARTIST_CAP)
    floor = float(scores[ranked[0]]) - MOOD_SIDE_MARGIN if len(ranked) else -np.inf
    picks.take(drawn.tolist(), scores, floor)
    if not picks.full:
        ranking = best_rows(scores, allowed, len(allowed)).tolist()
        picks.take(ranking, scores, floor)
        picks.side_cap = MOOD_SIZE
        picks.take(ranking, scores, floor)
    return smooth_order(index, scores, picks.rows)


def bonus(index: VectorIndex, ids: set[int], score: Matrix, amount: float) -> None:
    for track_id in ids:
        row = index.row_of.get(track_id)
        if row is not None:
            score[row] += amount


def radio_scores(
    index: VectorIndex,
    seed_row: int,
    recent: list[int],
    taste: RadioTaste,
    mood: Matrix | None,
) -> Matrix:
    seed = index.sound[seed_row]
    rows = [index.row_of[track_id] for track_id in recent if track_id in index.row_of]
    centre = unit(index.sound[rows[-RADIO_RECENT:]].mean(axis=0)) if rows else seed
    score = RADIO_SEED_WEIGHT * (index.sound @ seed) + RADIO_SESSION_WEIGHT * (index.sound @ centre)
    if mood is not None:
        score += RADIO_MOOD_WEIGHT * mood
    bonus(index, taste.mine, score, RADIO_LIKED_BONUS)
    bonus(index, taste.partner, score, RADIO_PARTNER_BONUS)
    return score


def draw_sound_radio(
    index: VectorIndex,
    seed_id: int,
    skip: set[int],
    recent: list[int],
    taste: RadioTaste,
    mood: Matrix | None,
    count: int,
    rnd: random.Random,
) -> list[Track]:
    seed_row = index.row_of[seed_id]
    score = radio_scores(index, seed_row, recent, taste, mood)
    for track_id in skip | {seed_id}:
        row = index.row_of.get(track_id)
        if row is not None:
            score[row] = -np.inf
    ranked = best_rows(score, np.flatnonzero(np.isfinite(score)), RADIO_POOL)
    drawn = ranked[sampled_order(score[ranked], RADIO_TEMPERATURE, rnd)]
    seed_artist = index.tracks[seed_row]["artist"]
    songs = unique_songs((index.tracks[row] for row in drawn.tolist()), {index.songs[seed_row]})
    return spaced(capped(songs, seed_artist, count), seed_artist)


def sound_topup(index: VectorIndex, ids: list[int], count: int) -> list[int]:
    rows = [index.row_of[track_id] for track_id in ids if track_id in index.row_of]
    if not rows:
        return []
    similar = index.sound @ unit(index.sound[rows].mean(axis=0))
    per = Counter(index.artist_of.get(track_id) for track_id in ids)
    have = set(ids)
    songs = {index.song_of[track_id] for track_id in ids if track_id in index.song_of}
    out: list[int] = []
    for row in np.argsort(-similar, kind="stable").tolist():
        track = index.tracks[row]
        song = index.songs[row]
        if track["id"] in have or song in songs or per[track["artist"]] >= TOPUP_ARTIST_CAP:
            continue
        songs.add(song)
        per[track["artist"]] += 1
        out.append(track["id"])
        if len(out) == count:
            break
    return out
