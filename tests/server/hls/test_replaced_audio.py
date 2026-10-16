import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from muse.catalog.infra.files import TrackFiles
from muse.settings import Settings
from muse.shared.mp3 import FrameReader
from tests.fixtures.catalog import FixtureCatalog
from tests.server.catalog.test_run import frames_of
from tests.server.hls.support import alice_over, open_session, session_id, shortest, split_tag

NEWER_NS = 5_000_000_000


class Swap:
    def __init__(self, song: int, target: Path, donor: bytes, copy: Path) -> None:
        self.song = song
        self.target = target
        self.donor = donor
        self.copy = copy

    def replace(self) -> None:
        before = self.target.stat().st_mtime_ns
        self.target.write_bytes(self.donor)
        os.utime(self.target, ns=(before + NEWER_NS, before + NEWER_NS))


def prepared(library: FixtureCatalog, tmp_path: Path) -> Swap:
    copy = tmp_path / "lib"
    shutil.copytree(library.library_dir, copy)
    (first, _, first_path), (_, _, donor_path) = shortest(library, 2, portable=True)
    target = copy / first_path.relative_to(library.library_dir)
    return Swap(first, target, donor_path.read_bytes(), copy)


def audio_of(segment: httpx.Response) -> list[bytes]:
    return frames_of(split_tag(segment.content)[1])


async def test_a_replaced_file_is_heard_on_the_next_segment(
    settings: Settings, library: FixtureCatalog, tmp_path: Path
) -> None:
    swap = prepared(library, tmp_path)
    old = FrameReader().push(swap.target.read_bytes())
    new = FrameReader().push(swap.donor)
    sid = session_id(30)

    async with alice_over(settings, swap.copy) as client:
        await open_session(client, sid, [swap.song])
        before = await client.get(f"/api/hls/{sid}/0/0.mp3")
        swap.replace()
        after = await client.get(f"/api/hls/{sid}/0/0.mp3")

    heard_before, heard_after = audio_of(before), audio_of(after)
    assert (before.status_code, after.status_code) == (200, 200)
    assert heard_before[: len(old)] == old[: len(heard_before)]
    assert heard_after[: len(new)] == new[: len(heard_after)]
    assert heard_after != heard_before


async def test_a_segment_answers_304_to_its_own_etag_until_the_file_changes(
    settings: Settings,
    library: FixtureCatalog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swap = prepared(library, tmp_path)
    sid = session_id(31)
    opened: list[str | None] = []
    original = TrackFiles.open_audio

    def counting(self: TrackFiles, stored: str | None) -> Any:
        opened.append(stored)
        return original(self, stored)

    async with alice_over(settings, swap.copy) as client:
        await open_session(client, sid, [swap.song])
        first = await client.get(f"/api/hls/{sid}/0/0.mp3")
        etag = first.headers["etag"]
        monkeypatch.setattr(TrackFiles, "open_audio", counting)
        same = await client.get(f"/api/hls/{sid}/0/0.mp3", headers={"If-None-Match": etag})
        weak = await client.get(f"/api/hls/{sid}/0/0.mp3", headers={"If-None-Match": f"W/{etag}"})
        listed = await client.get(
            f"/api/hls/{sid}/0/0.mp3", headers={"If-None-Match": f'"other", {etag}'}
        )
        monkeypatch.setattr(TrackFiles, "open_audio", original)
        other = await client.get(f"/api/hls/{sid}/0/0.mp3", headers={"If-None-Match": '"other"'})
        swap.replace()
        changed = await client.get(f"/api/hls/{sid}/0/0.mp3", headers={"If-None-Match": etag})

    assert first.status_code == 200
    assert [(r.status_code, r.content) for r in (same, weak, listed)] == [(304, b"")] * 3
    assert {r.headers["etag"] for r in (same, weak, listed)} == {etag}
    assert {r.headers["cache-control"] for r in (first, same, weak, listed)} == {
        "private, no-cache"
    }
    assert opened == []
    assert (other.status_code, other.headers["etag"]) == (200, etag)
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert changed.headers["cache-control"] == "private, no-cache"


async def test_a_stream_answers_304_to_its_own_etag_until_the_file_changes(
    settings: Settings, library: FixtureCatalog, tmp_path: Path
) -> None:
    swap = prepared(library, tmp_path)
    url = f"/api/stream/{swap.song}"

    async with alice_over(settings, swap.copy) as client:
        first = await client.get(url)
        etag = first.headers["etag"]
        same = await client.get(url, headers={"If-None-Match": etag})
        ranged = await client.get(url, headers={"If-None-Match": etag, "Range": "bytes=0-9"})
        swap.replace()
        whole = await client.get(url, headers={"If-None-Match": etag})
        part = await client.get(url, headers={"If-None-Match": etag, "Range": "bytes=0-9"})

    assert first.status_code == 200
    assert (same.status_code, same.content) == (304, b"")
    assert (ranged.status_code, ranged.content) == (304, b"")
    assert {r.headers["etag"] for r in (same, ranged)} == {etag}
    assert {r.headers["cache-control"] for r in (first, same, whole, part)} == {"private, no-cache"}
    assert (whole.status_code, whole.content) == (200, swap.donor)
    assert whole.headers["etag"] != etag
    assert (part.status_code, part.content) == (206, swap.donor[:10])
    assert part.headers["etag"] == whole.headers["etag"]
    assert part.headers["cache-control"] == "private, no-cache"
