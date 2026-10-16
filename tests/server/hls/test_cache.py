from collections.abc import Awaitable, Callable

import anyio
import pytest

from muse.hls.domain import SONG_CACHE, SongAudio
from muse.hls.service import SongCache


def builder(built: list[int], key: int) -> Callable[[], Awaitable[SongAudio]]:
    async def build() -> SongAudio:
        built.append(key)
        await anyio.sleep(0.01)
        return SongAudio(bytes([key]), (0, 1))

    return build


def test_the_cache_keeps_the_last_eight_songs() -> None:
    assert SongCache().size == SONG_CACHE == 8


async def test_the_least_recently_used_song_is_dropped_first() -> None:
    cache, built = SongCache(), []
    cache.size = 2

    for key in (1, 2, 1, 3, 1, 2):
        await cache.get((key, 0, 0, 0, 0), builder(built, key))

    assert built == [1, 2, 3, 2]
    assert list(cache.songs) == [(1, 0, 0, 0, 0), (2, 0, 0, 0, 0)]


async def test_simultaneous_requests_for_one_song_read_it_once() -> None:
    cache, built = SongCache(), []
    answers: list[SongAudio] = []

    async def ask() -> None:
        answers.append(await cache.get((7, 0, 0, 0, 0), builder(built, 7)))

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(ask)

    assert built == [7]
    assert len(answers) == 5
    assert all(answer is answers[0] for answer in answers)
    assert cache.building == {}


async def test_a_failed_build_caches_nothing_and_is_tried_again() -> None:
    cache, built = SongCache(), []

    async def broken() -> SongAudio:
        raise OSError("gone")

    with pytest.raises(OSError, match="gone"):
        await cache.get((1, 0, 0, 0, 0), broken)
    await cache.get((1, 0, 0, 0, 0), builder(built, 1))

    assert built == [1]
    assert cache.building == {}
