import random
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise

import numpy as np

from muse.catalog.domain import LibraryTrack
from muse.discovery.domain import (
    SHELF_GROUPS,
    SHELF_MOODS,
    RadioTaste,
    RawVectors,
    mood_shelves,
    song_key,
)
from muse.discovery.draws import (
    MOOD_SIZE,
    MOSAIC_WINDOW,
    draw_mood,
    draw_sound_radio,
    mosaic_rows,
    smooth_order,
    sound_topup,
    spread_mosaics,
)
from muse.discovery.vectors import VectorIndex, build_index, grouped
from tests.server.discovery.support import (
    DIM,
    blob,
    clustered,
    mood_row,
    space,
    track,
)

NOBODY = RadioTaste(set(), set())


def ids_in(index: VectorIndex, rows: list[int], prefix: str) -> int:
    return sum(index.tracks[row]["artist"].startswith(prefix) for row in rows)


def two_clusters() -> tuple[VectorIndex, list[LibraryTrack]]:
    return clustered([((0,), 1000, False, 100), ((1,), 1000, False, 100)])


def test_a_mood_draw_is_seeded_sized_artist_capped_and_in_the_mood() -> None:
    index, _ = two_clusters()
    scores = index.mood_space @ np.eye(DIM)[0]

    first = draw_mood(index, scores, None, random.Random("calm:2026-01-01"))
    again = draw_mood(index, scores, None, random.Random("calm:2026-01-01"))
    tomorrow = draw_mood(index, scores, None, random.Random("calm:2026-01-02"))

    assert first == again
    assert set(first) != set(tomorrow)
    assert len(first) == len(set(first)) == MOOD_SIZE
    per = Counter(index.tracks[row]["artist"] for row in first)
    assert max(per.values()) <= 3
    assert ids_in(index, first, "Artist 0-") == MOOD_SIZE


def test_neither_vocal_side_passes_three_quarters_unless_the_mood_picks_a_side() -> None:
    index, _ = clustered([((0,), 900, True, 300), ((0,), 100, False, 100)])
    scores = index.mood_space @ np.eye(DIM)[0]

    rows = draw_mood(index, scores, None, random.Random("quota"))

    russian = int(index.russian[rows].sum())
    assert (russian, len(rows) - russian) == (150, 50)
    only_ru = draw_mood(index, scores, "ru", random.Random("quota"))
    assert len(only_ru) == MOOD_SIZE
    assert index.russian[only_ru].all()
    only_foreign = draw_mood(index, scores, "foreign", random.Random("quota"))
    assert len(only_foreign) == 100
    assert not index.russian[only_foreign].any()


def test_the_minority_side_is_filled_only_from_close_matches_else_the_imbalance_stays() -> None:
    weak, _ = clustered([((0,), 900, True, 300), ((1,), 100, False, 100)])
    mixed, _ = clustered([((0,), 900, True, 300), ((0,), 30, False, 30), ((1,), 100, False, 100)])

    far = draw_mood(weak, weak.mood_space @ np.eye(DIM)[0], None, random.Random("far"))
    near = draw_mood(mixed, mixed.mood_space @ np.eye(DIM)[0], None, random.Random("near"))

    assert len(far) == MOOD_SIZE
    assert not (~weak.russian[far]).any()
    assert len(near) == MOOD_SIZE
    assert 20 <= int((~mixed.russian[near]).sum()) <= 30
    assert ids_in(mixed, near, "Artist 1-") == 0


def test_a_mosaic_is_the_best_scoring_songs_of_the_mood_side_in_rank_order() -> None:
    index, _ = clustered([((0,), 700, True, 100), ((0,), 700, False, 100), ((1,), 300, False, 100)])
    scores = index.mood_space @ np.eye(DIM)[0]

    anyone = mosaic_rows(index, scores, None)
    russian = mosaic_rows(index, scores, "ru")
    foreign = mosaic_rows(index, scores, "foreign")

    assert len(anyone) == len(russian) == len(foreign) == MOSAIC_WINDOW
    assert [scores[row] for row in anyone] == sorted((scores[row] for row in anyone), reverse=True)
    assert index.russian[russian].all()
    assert not index.russian[foreign].any()
    assert ids_in(index, foreign, "Artist 1-") == 0
    assert min(scores[row] for row in anyone) >= np.sort(scores)[-MOSAIC_WINDOW]


def test_mosaics_give_each_mood_its_best_albums_but_no_album_more_than_twice() -> None:
    ranked = [[1, 2, 3, 4, 5], [1, 2, 3, 6], [1, 2, 7, 8, 9, 10], [1, 2, 3, 4, 11], [12]]

    mosaics = spread_mosaics(ranked)

    assert mosaics == [[1, 2, 3, 4], [1, 2, 3, 6], [7, 8, 9, 10], [4, 11], [12]]
    assert max(Counter(album for mosaic in mosaics for album in mosaic).values()) == 2
    assert spread_mosaics([]) == []


def test_a_small_library_gives_every_track_it_can_within_the_artist_cap() -> None:
    index, _ = clustered([((0,), 30, False, 5), ((1,), 20, False, 20)])
    scores = index.mood_space @ np.eye(DIM)[0]

    rows = draw_mood(index, scores, None, random.Random("small"))

    assert len(rows) == len(set(rows)) == 15 + 20
    assert ids_in(index, rows, "Artist 0-") == 15


def test_smoothing_keeps_the_set_starts_at_the_best_track_and_flows() -> None:
    index, _ = two_clusters()
    scores = index.mood_space @ np.eye(DIM)[0]
    picks = random.Random(1).sample(range(index.size), 120)

    ordered = smooth_order(index, scores, picks)

    assert sorted(ordered) == sorted(picks)
    assert ordered[0] == max(picks, key=lambda row: scores[row])

    def jumps(order: list[int]) -> float:
        return float(np.mean([index.sound[a] @ index.sound[b] for a, b in pairwise(order)]))

    shuffled = random.Random(2).sample(picks, len(picks))
    assert jumps(ordered) > jumps(shuffled)
    assert smooth_order(index, scores, [3]) == [3]
    assert smooth_order(index, scores, []) == []


def test_one_song_on_several_albums_comes_up_once_in_a_mood_a_radio_and_a_top_up() -> None:
    index, tracks = clustered([((0,), 1200, False, 100), ((1,), 300, False, 100)], copies=3)
    scores = index.mood_space @ np.eye(DIM)[0]

    mood = draw_mood(index, scores, None, random.Random("dupes"))
    radio = drawn(index, 1, count=30)
    extra = sound_topup(index, [1, 2, 3], 40)

    assert len(mood) == MOOD_SIZE
    assert len({index.songs[row] for row in mood}) == MOOD_SIZE
    assert len({song_key(item["artist"], item["title"]) for item in radio}) == len(radio) == 30
    assert song_key(tracks[0]["artist"], tracks[0]["title"]) not in {
        song_key(item["artist"], item["title"]) for item in radio
    }
    assert len({index.song_of[track_id] for track_id in extra}) == len(extra) == 40
    assert not {index.song_of[track_id] for track_id in extra} & {index.song_of[1]}


def test_a_song_key_ignores_case_versions_and_featured_artists() -> None:
    plain = song_key("Nautilus", "Я хочу быть с тобой")

    assert plain == song_key(
        "НАУТИЛУС ".strip().replace("НАУТИЛУС", "nautilus"), "я хочу быть с тобой"
    )
    for title in (
        "Я хочу быть с тобой (Remastered 2011)",
        "Я хочу быть с тобой - Live",
        "Я хочу быть с тобой - Live at Wembley 1985",
        "Я хочу быть с тобой [Deluxe]",
        "Я хочу быть с тобой (feat. Кто-то)",
        "Я хочу быть с тобой feat. Кто-то",
    ):
        assert song_key("Nautilus", title) == plain
    assert song_key("Nautilus", "Я хочу быть с тобой - Part 2") != plain
    assert song_key("Другой", "Я хочу быть с тобой") != plain


def three_clusters() -> tuple[VectorIndex, list[LibraryTrack]]:
    moods = [mood_row("a", "G", 1, 0, None), mood_row("b", "G", 2, 1, None)]
    return clustered(
        [
            ((0,), 300, False, 100),
            ((1,), 300, False, 100),
            ((2,), 300, False, 100),
            ((0, 1), 1, False, 1),
        ],
        moods,
    )


def drawn(
    index: VectorIndex,
    seed_id: int,
    skip: set[int] | None = None,
    recent: list[int] | None = None,
    mood: str | None = None,
    taste: RadioTaste = NOBODY,
    count: int = 25,
    seed: int = 0,
) -> list[LibraryTrack]:
    scores = index.scores(index.by_id[mood]) if mood else None
    rnd = random.Random(seed)
    picked = draw_sound_radio(
        index, seed_id, skip or set(), recent or [], taste, scores, count, rnd
    )
    return [index.tracks[index.row_of[item["id"]]] for item in picked]


def share(tracks: list[LibraryTrack], prefix: str) -> float:
    return sum(item["artist"].startswith(prefix) for item in tracks) / len(tracks)


def test_radio_stays_with_the_sound_of_its_seed_and_skips_every_negative() -> None:
    index, _ = three_clusters()
    seed_id = 1
    negatives = set(range(2, 250))

    plain = drawn(index, seed_id)
    avoiding = drawn(index, seed_id, skip=negatives)

    assert len(plain) == len({item["id"] for item in plain}) == 25
    assert share(plain, "Artist 0-") == 1
    assert seed_id not in {item["id"] for item in plain}
    assert not negatives & {item["id"] for item in avoiding}
    assert len(avoiding) == 25
    assert share(avoiding, "Artist 0-") == 1


def test_the_played_queue_pulls_the_radio_towards_where_the_session_went() -> None:
    index, tracks = three_clusters()
    between = tracks[-1]["id"]
    zero = [item["id"] for item in tracks if item["artist"].startswith("Artist 0-")]
    one = [item["id"] for item in tracks if item["artist"].startswith("Artist 1-")]

    towards_one = drawn(index, between, recent=[*zero[:20], *one[:6]])
    towards_zero = drawn(index, between, recent=[*one[:20], *zero[:6]])

    assert share(towards_one, "Artist 1-") == 1
    assert share(towards_zero, "Artist 0-") == 1


def test_a_mood_tilts_the_radio_between_equally_near_sounds() -> None:
    index, tracks = three_clusters()
    between = tracks[-1]["id"]

    assert share(drawn(index, between, mood="a"), "Artist 0-") == 1
    assert share(drawn(index, between, mood="b"), "Artist 1-") == 1


def test_likes_only_break_ties_and_my_likes_count_more_than_my_partners() -> None:
    index, tracks = clustered([((0,), 300, False, 100), ((1,), 300, False, 100)], noise=0.01)
    zero = [item["id"] for item in tracks if item["artist"].startswith("Artist 0-")][1:]
    taste = RadioTaste(mine=set(zero[:20]), partner=set(zero[20:40]))
    seen: Counter[str] = Counter()
    for seed in range(120):
        for item in drawn(index, 1, taste=taste, seed=seed, count=10):
            kind = "mine" if item["id"] in taste.mine else "partner"
            seen["other" if item["id"] not in taste.mine | taste.partner else kind] += 1

    assert seen["mine"] / 20 > seen["partner"] / 20 > seen["other"] / (len(zero) - 40)


def test_radio_keeps_three_songs_of_the_seed_artist_two_of_the_rest_and_spaces_them() -> None:
    index, tracks = clustered([((0,), 330, False, 11), ((1,), 100, False, 100)])

    picked = drawn(index, 1, count=25)

    per = Counter(item["artist"] for item in picked)
    seed_artist = tracks[0]["artist"]
    assert per[seed_artist] == 3
    assert max(count for artist, count in per.items() if artist != seed_artist) == 2
    assert sum(item["artist"].startswith("Artist 0-") for item in picked) == 3 + 10 * 2
    assert len(picked) == 25
    assert all(a["artist"] != b["artist"] for a, b in pairwise(picked))


def test_a_sound_top_up_adds_the_nearest_tracks_without_repeats_or_a_fourth_by_one_artist() -> None:
    index, tracks = two_clusters()
    have = [item["id"] for item in tracks if item["artist"].startswith("Artist 0-")][:12]

    extra = sound_topup(index, have, 60)

    assert len(extra) == len(set(extra)) == 60
    assert not set(have) & set(extra)
    per = Counter(index.artist_of[track_id] for track_id in [*have, *extra])
    assert max(per.values()) <= 3
    assert all(index.artist_of[track_id].startswith("Artist 0-") for track_id in extra)
    assert sound_topup(index, have, 60) == extra
    assert sound_topup(index, [999999], 5) == []


def test_an_index_needs_the_whole_space_and_only_keeps_playable_tracks_with_vectors() -> None:
    tracks = [track(1, "A", "One"), track(2, "B", "Two"), track(3, "C", "Three")]
    vec = np.eye(DIM)[0]
    rows = [(1, blob(vec)), (2, blob(vec[:3])), (99, blob(vec))]
    raw = RawVectors([key for key, _ in rows], [value for _, value in rows], space(), [])

    index = build_index(raw, tracks)
    broken = build_index(RawVectors([1], [blob(vec)], {"dim": b"8"}, []), tracks)

    assert [item["id"] for item in index.tracks] == [1]
    assert index.artist_of == {1: "A", 2: "B", 3: "C"}
    assert index.version == "test-1"
    assert index.query_scores([1.0]) is None
    assert index.query_scores([1.0, *([0.0] * (DIM - 1))]) is not None
    assert (broken.size, broken.version, broken.moods) == (0, "", [])
    assert build_index(RawVectors([], [], {}, []), tracks).size == 0


def test_moods_come_in_file_order_grouped_with_their_vocal_side() -> None:
    moods = [
        mood_row("late", "Ночь", 3, 0, None),
        mood_row("gym", "Спорт", 1, 1, "foreign"),
        mood_row("dusk", "Ночь", 2, 0, "ru"),
    ]
    index, _ = clustered([((0,), 10, False, 10), ((1,), 10, False, 10)], moods)

    assert [mood.id for mood in index.moods] == ["gym", "dusk", "late"]
    assert index.by_id["dusk"].lang == "ru"
    assert index.mood_scores.shape == (3, 20)
    assert [(title, [mood.id for mood in group]) for title, group in grouped(index.moods)] == [
        ("Спорт", ["gym"]),
        ("Ночь", ["dusk", "late"]),
    ]


def test_the_home_shelves_rotate_through_groups_and_moods_day_by_day() -> None:
    groups = [(f"G{number}", [f"{number}-{mood}" for mood in range(10)]) for number in range(5)]
    day = date(2026, 1, 15)

    today = mood_shelves(groups, day)
    tomorrow = mood_shelves(groups, day + timedelta(days=1))

    assert len(today) == SHELF_GROUPS
    assert all(len(moods) == SHELF_MOODS for _, moods in today)
    assert [title for title, _ in today] != [title for title, _ in tomorrow]
    assert {title for title, _ in today} | {title for title, _ in tomorrow} <= {
        "G0",
        "G1",
        "G2",
        "G3",
        "G4",
    }
    for title, shelf in today:
        moods = dict(groups)[title]
        start = moods.index(shelf[0])
        assert shelf == [*moods[start:], *moods[:start]][:SHELF_MOODS]
    assert mood_shelves(groups, day) == today
    later = mood_shelves(groups, day + timedelta(days=5))
    assert [title for title, _ in later] == [title for title, _ in today]
    assert [moods[0] for _, moods in later] != [moods[0] for _, moods in today]
    few = mood_shelves([("Only", ["a", "b", "c"])], day)
    assert [(title, sorted(moods)) for title, moods in few] == [("Only", ["a", "b", "c"])]
    assert mood_shelves([], day) == []
