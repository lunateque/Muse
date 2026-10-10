import asyncio
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from litestar.testing import AsyncTestClient
from structlog.testing import capture_logs

from muse.app import create_app
from muse.catalog.infra.files import TrackFiles
from muse.hls.domain import SEG_FRAMES
from muse.settings import Settings
from muse.shared.mp3 import STREAM_KIND, FrameReader, silent_frame
from tests.fixtures.catalog import FixtureCatalog
from tests.server.catalog.support import fixture_rows
from tests.server.catalog.test_run import frames_of
from tests.server.hls.support import (
    open_session,
    playlist_entries,
    session_id,
    shortest,
    split_tag,
    stamp,
)
from tests.server.support import ORIGIN, WRITE, signed_in

PER_SECOND = STREAM_KIND.rate / STREAM_KIND.samples
TICKS_PER_FRAME = 2160
FRAME = STREAM_KIND.samples / STREAM_KIND.rate


def stamp_of(segment: bytes) -> int:
    tag, _ = split_tag(segment)
    return stamp(tag)


async def fetch_all(client: httpx.AsyncClient, sid: str) -> list[httpx.Response]:
    playlist = await client.get(f"/api/hls/{sid}.m3u8")
    assert playlist.status_code == 200
    return [await client.get(f"/api/hls/{uri}") for _, uri in playlist_entries(playlist.text)]


@asynccontextmanager
async def alice_over(settings: Settings, library_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    moved = settings.model_copy(
        update={"paths": settings.paths.model_copy(update={"library_dir": library_dir})}
    )
    application = create_app(moved)
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        client = await signed_in(application, "alice")
        try:
            yield client
        finally:
            await client.aclose()


async def test_a_session_is_created_and_its_playlist_lists_the_songs_minus_the_start(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, first_dur, _), (second, second_dur, _) = shortest(library, 2)
    sid = session_id(1)

    created = await open_session(alice, sid, [first, second], at=2.5)
    playlist = await alice.get(created.json()["url"])

    assert created.status_code == 201
    assert created.json() == {
        "id": sid,
        "url": f"/api/hls/{sid}.m3u8",
        "songs": [{"id": first, "dur": first_dur}, {"id": second, "dur": second_dur}],
    }
    assert playlist.status_code == 200
    assert playlist.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert playlist.headers["cache-control"] == "no-cache"
    lines = playlist.text.splitlines()
    assert lines[:2] == ["#EXTM3U", "#EXT-X-VERSION:3"]
    assert "#EXT-X-ENDLIST" not in lines
    entries = playlist_entries(playlist.text)
    total = first_dur + second_dur - 2.5
    assert abs(sum(float(length) for length, _ in entries) - total) <= FRAME
    first_frames = round((first_dur - 2.5) * PER_SECOND)
    second_frames = round(total * PER_SECOND) - first_frames
    per_song = [-(-first_frames // SEG_FRAMES), -(-second_frames // SEG_FRAMES)]
    assert [uri.split("/")[1] for _, uri in entries].count("0") == per_song[0]
    assert len(entries) == sum(per_song)
    assert entries[0] == (f"{min(first_frames, SEG_FRAMES) / PER_SECOND:.3f}", f"{sid}/0/0.mp3")


async def test_the_segments_of_a_session_join_into_exactly_the_run(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, _, _), (second, _, _) = shortest(library, 2)
    sid = session_id(2)
    await open_session(alice, sid, [first, second], at=1.0)

    segments = await fetch_all(alice, sid)
    run = await alice.get(f"/api/run?ids={first},{second}&at=1.0")

    assert {item.status_code for item in segments} == {200}
    assert {item.headers["content-type"] for item in segments} == {"audio/mpeg"}
    assert {item.headers["cache-control"] for item in segments} == {"private, max-age=86400"}
    joined = b"".join(split_tag(item.content)[1] for item in segments)
    assert joined == run.content


async def test_segment_timestamps_are_continuous_over_a_song_boundary(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, first_dur, _), (second, second_dur, _) = shortest(library, 2)
    sid = session_id(3)
    await open_session(alice, sid, [first, second])

    segments = await fetch_all(alice, sid)

    counts = [len(frames_of(split_tag(item.content)[1])) for item in segments]
    starts = [sum(counts[:number]) * TICKS_PER_FRAME for number in range(len(counts))]
    assert [stamp_of(item.content) for item in segments] == starts
    first_frames = round(first_dur * PER_SECOND)
    assert sum(counts) == round((first_dur + second_dur) * PER_SECOND)
    song_boundary = -(-first_frames // SEG_FRAMES)
    assert counts[song_boundary - 1] == first_frames - (song_boundary - 1) * SEG_FRAMES
    assert counts[song_boundary - 1] < SEG_FRAMES or first_frames % SEG_FRAMES == 0
    assert starts[song_boundary] == first_frames * TICKS_PER_FRAME
    assert all(count == SEG_FRAMES for count in counts[: song_boundary - 1])


async def test_a_short_file_is_padded_with_silence_up_to_the_song_length(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, first_dur, path)] = shortest(library, 1)
    sid = session_id(4)
    await open_session(alice, sid, [first])
    native = FrameReader().push(path.read_bytes())

    segments = await fetch_all(alice, sid)

    frames = frames_of(b"".join(split_tag(item.content)[1] for item in segments))
    assert len(frames) == round(first_dur * PER_SECOND)
    assert frames[: len(native)] == native
    assert frames[len(native) :] == [silent_frame(STREAM_KIND)] * (len(frames) - len(native))


async def test_a_start_second_skips_whole_frames_of_the_first_song(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, first_dur, path)] = shortest(library, 1)
    sid = session_id(5)
    await open_session(alice, sid, [first], at=0.1)
    native = FrameReader().push(path.read_bytes())

    segments = await fetch_all(alice, sid)

    frames = frames_of(b"".join(split_tag(item.content)[1] for item in segments))
    skipped = round(0.1 * PER_SECOND)
    assert len(frames) == round((first_dur - 0.1) * PER_SECOND)
    assert frames[: len(native) - skipped] == native[skipped:]


async def test_segments_and_playlist_need_no_login_and_unknown_ones_are_404(
    alice: httpx.AsyncClient, client: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    sid = session_id(6)
    await open_session(alice, sid, [first])

    answers = {
        "playlist": (await client.get(f"/api/hls/{sid}.m3u8")).status_code,
        "segment": (await client.get(f"/api/hls/{sid}/0/0.mp3")).status_code,
        "song": (await client.get(f"/api/hls/{sid}/1/0.mp3")).status_code,
        "index": (await client.get(f"/api/hls/{sid}/0/99.mp3")).status_code,
        "negative song": (await client.get(f"/api/hls/{sid}/-1/0.mp3")).status_code,
        "name": (await client.get(f"/api/hls/{sid}/0/x.mp3")).status_code,
        "session": (await client.get(f"/api/hls/{session_id(99)}/0/0.mp3")).status_code,
    }

    assert answers == {
        "playlist": 200,
        "segment": 200,
        "song": 404,
        "index": 404,
        "negative song": 404,
        "name": 404,
        "session": 404,
    }


async def test_creating_needs_a_login_and_an_origin(
    client: httpx.AsyncClient, alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    body = {"ids": [first], "at": 0}

    anonymous = await client.put(f"/api/hls/{session_id(7)}", json=body, headers=WRITE)
    foreign = await alice.put(
        f"/api/hls/{session_id(7)}", json=body, headers={"Origin": "https://evil.example"}
    )

    assert anonymous.status_code == 401
    assert (foreign.status_code, foreign.json()) == (403, {"detail": "bad origin"})


async def test_the_same_owner_putting_an_id_again_gets_the_session_unchanged(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, _, _), (second, _, _) = shortest(library, 2)
    sid = session_id(8)

    created = await open_session(alice, sid, [first], at=1.0)
    again = await open_session(alice, sid, [second], at=3.0)
    playlist = await alice.get(f"/api/hls/{sid}.m3u8")

    assert (created.status_code, again.status_code) == (201, 200)
    assert again.json() == created.json()
    assert [song["id"] for song in again.json()["songs"]] == [first]
    assert playlist_entries(playlist.text)[0][1] == f"{sid}/0/0.mp3"


async def test_an_id_owned_by_someone_else_is_404_for_everything_but_reading(
    alice: httpx.AsyncClient, bob: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, _, _), (second, _, _) = shortest(library, 2)
    sid = session_id(9)
    await open_session(alice, sid, [first])

    taken = await open_session(bob, sid, [first])
    appended = await bob.post(
        f"/api/hls/{sid}/append", json={"ids": [second], "end": True}, headers=WRITE
    )
    watched = await bob.get(f"/api/hls/{sid}.m3u8")

    assert (taken.status_code, taken.json()) == (404, {"detail": "no session"})
    assert (appended.status_code, appended.json()) == (404, {"detail": "no session"})
    assert watched.status_code == 200
    assert "#EXT-X-ENDLIST" not in watched.text


async def test_a_session_that_cannot_be_opened_is_refused(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, first_dur, _)] = shortest(library, 1)
    untimed = fixture_untimed(library)
    cases: dict[str, tuple[str, dict[str, Any], int, str]] = {
        "unknown": (session_id(10), {"ids": [999999], "at": 0}, 404, "no track"),
        "untimed": (session_id(11), {"ids": [untimed], "at": 0}, 404, "no track"),
        "past the end": (session_id(12), {"ids": [first], "at": first_dur}, 400, "bad run"),
    }

    answers = {}
    for name, (sid, body, _, _) in cases.items():
        response = await alice.put(f"/api/hls/{sid}", json=body, headers=WRITE)
        answers[name] = (response.status_code, response.json()["detail"])

    assert answers == {name: (status, detail) for name, (_, _, status, detail) in cases.items()}


def fixture_untimed(library: FixtureCatalog) -> int:
    sql = "SELECT id FROM tracks WHERE status='ok' AND dur IS NULL"
    return fixture_rows(library, sql)[0]["id"]


async def test_a_malformed_request_is_422(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    valid = session_id(13)
    attempts = {
        "short id": (session_id(1)[:21], {"ids": [first], "at": 0}),
        "id with a dot": ("a" * 21 + ".", {"ids": [first], "at": 0}),
        "long id": ("a" * 65, {"ids": [first], "at": 0}),
        "no ids": (valid, {"ids": [], "at": 0}),
        "too many ids": (valid, {"ids": [first] * 501, "at": 0}),
        "bad id": (valid, {"ids": [0], "at": 0}),
        "negative at": (valid, {"ids": [first], "at": -1}),
        "no at": (valid, {"ids": [first]}),
    }

    statuses = {}
    for name, (sid, body) in attempts.items():
        statuses[name] = (await alice.put(f"/api/hls/{sid}", json=body, headers=WRITE)).status_code

    assert set(statuses.values()) == {422}
    refused = await alice.put(
        f"/api/hls/{'a' * 21}!", json={"ids": [first], "at": 0}, headers=WRITE
    )
    assert refused.json() == {"detail": "bad session id"}


async def test_the_list_is_cut_at_the_first_song_that_cannot_be_played(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, first_dur, _), (second, _, _) = shortest(library, 2)

    response = await open_session(alice, session_id(14), [first, 999999, second])

    assert response.json()["songs"] == [{"id": first, "dur": first_dur}]


async def test_a_session_whose_first_file_is_gone_is_404(
    settings: Settings, library: FixtureCatalog, tmp_path: Path
) -> None:
    [(first, _, _)] = shortest(library, 1)
    empty = tmp_path / "empty-library"
    empty.mkdir()

    async with alice_over(settings, empty) as client:
        gone = await open_session(client, session_id(15), [first])

    assert (gone.status_code, gone.json()) == (404, {"detail": "file gone"})


async def test_appending_adds_songs_and_end_closes_the_playlist(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    (first, first_dur, _), (second, second_dur, _) = shortest(library, 2)
    sid = session_id(16)
    await open_session(alice, sid, [first])
    before = playlist_entries((await alice.get(f"/api/hls/{sid}.m3u8")).text)

    appended = await alice.post(
        f"/api/hls/{sid}/append", json={"ids": [second, 999999, first]}, headers=WRITE
    )
    middle = await alice.get(f"/api/hls/{sid}.m3u8")
    closed = await alice.post(
        f"/api/hls/{sid}/append", json={"ids": [first], "end": True}, headers=WRITE
    )
    last = await alice.get(f"/api/hls/{sid}.m3u8")

    assert appended.status_code == 200
    assert appended.json() == {"songs": [{"id": second, "dur": second_dur}]}
    assert appended.headers["cache-control"] == "no-store"
    entries = playlist_entries(middle.text)
    assert entries[: len(before)] == before
    assert abs(sum(float(length) for length, _ in entries) - (first_dur + second_dur)) <= FRAME
    assert "#EXT-X-ENDLIST" not in middle.text
    assert closed.json() == {"songs": [{"id": first, "dur": first_dur}]}
    assert last.text.splitlines()[-1] == "#EXT-X-ENDLIST"
    assert playlist_entries(last.text)[: len(entries)] == entries


async def test_a_session_holds_at_most_500_songs(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, first_dur, _)] = shortest(library, 1)
    sid = session_id(17)
    await open_session(alice, sid, [first] * 499)

    results = [
        (
            await alice.post(f"/api/hls/{sid}/append", json={"ids": [first] * 3}, headers=WRITE)
        ).json()
        for _ in range(2)
    ]

    assert results == [{"songs": [{"id": first, "dur": first_dur}]}, {"songs": []}]


async def test_append_is_validated_and_unknown_sessions_are_404(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    sid = session_id(18)
    await open_session(alice, sid, [first])

    statuses = [
        (await alice.post(f"/api/hls/{sid}/append", json=body, headers=WRITE)).status_code
        for body in ({"ids": []}, {"ids": [first] * 201}, {"ids": [-4]})
    ]
    unknown = await alice.post(
        f"/api/hls/{session_id(98)}/append", json={"ids": [first]}, headers=WRITE
    )

    assert statuses == [422, 422, 422]
    assert unknown.status_code == 404


async def test_the_playlist_waits_for_a_session_that_is_about_to_appear(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    sid = session_id(19)

    waiting = asyncio.create_task(alice.get(f"/api/hls/{sid}.m3u8"))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    created = await open_session(alice, sid, [first])
    playlist = await asyncio.wait_for(waiting, timeout=3)

    assert (created.status_code, playlist.status_code) == (201, 200)
    assert playlist_entries(playlist.text)[0][1] == f"{sid}/0/0.mp3"


async def test_the_playlist_of_a_session_that_never_appears_is_404_after_the_wait(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("muse.hls.service.PLAYLIST_WAIT_S", 0.3)
    started = time.monotonic()

    response = await client.get(f"/api/hls/{session_id(20)}.m3u8")

    assert (response.status_code, response.json()) == (404, {"detail": "no session"})
    assert time.monotonic() - started >= 0.3


async def test_a_name_that_cannot_be_a_session_is_404_without_waiting(
    client: httpx.AsyncClient,
) -> None:
    started = time.monotonic()

    answers = [
        (await client.get(f"/api/hls/{name}")).status_code
        for name in ("short.m3u8", f"{session_id(21)}.txt", f"{session_id(21)}")
    ]

    assert answers == [404, 404, 404]
    assert time.monotonic() - started < 1


async def test_a_song_is_read_from_its_file_once_for_all_its_segments(
    alice: httpx.AsyncClient, library: FixtureCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql = "SELECT id FROM tracks WHERE status='ok' AND dur > 60 ORDER BY dur DESC, id LIMIT 1"
    [first] = [row["id"] for row in fixture_rows(library, sql)]
    sid = session_id(22)
    await open_session(alice, sid, [first])
    opened: list[str | None] = []
    original = TrackFiles.open_audio

    def counting(self: TrackFiles, stored: str | None) -> Any:
        opened.append(stored)
        return original(self, stored)

    monkeypatch.setattr(TrackFiles, "open_audio", counting)

    segments = await fetch_all(alice, sid)
    again = await alice.get(f"/api/hls/{sid}/0/1.mp3")

    assert len(segments) > 1
    assert len(opened) == 1
    assert again.content == segments[1].content


async def test_a_segment_whose_file_vanished_is_503_and_logged(
    settings: Settings, library: FixtureCatalog, tmp_path: Path
) -> None:
    copy = tmp_path / "lib"
    shutil.copytree(library.library_dir, copy)
    (first, _, _), (second, _, second_path) = shortest(library, 2, portable=True)
    sid = session_id(23)

    async with alice_over(settings, copy) as client:
        await open_session(client, sid, [first, second])
        (copy / second_path.relative_to(library.library_dir)).unlink()
        playlist = await client.get(f"/api/hls/{sid}.m3u8")
        with capture_logs() as logs:
            ok = await client.get(f"/api/hls/{sid}/0/0.mp3")
            broken = await client.get(f"/api/hls/{sid}/1/0.mp3")

    assert playlist.status_code == 200
    assert (ok.status_code, broken.status_code) == (200, 503)
    assert broken.json() == {"detail": "segment unavailable"}
    failures = [entry for entry in logs if entry["event"] == "hls segment"]
    assert [
        (entry["track"], entry["song"], entry["index"], entry["why"]) for entry in failures
    ] == [(second, 1, 0, "file gone")]


async def test_hls_requests_go_to_the_stream_log(
    alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    [(first, _, _)] = shortest(library, 1)
    sid = session_id(24)

    with capture_logs() as logs:
        await open_session(alice, sid, [first])
        playlist = await alice.get(f"/api/hls/{sid}.m3u8?reload=1", headers={"Range": "bytes=0-9"})
        segment = await alice.get(f"/api/hls/{sid}/0/0.mp3")

    entries = [entry for entry in logs if entry["event"] == "stream request"]
    assert [
        (entry["track"], entry["query"], entry["status"], entry["range"], entry["user"])
        for entry in entries
    ] == [
        ("hls", sid, 201, None, "alice"),
        ("hls", f"{sid}.m3u8?reload=1", 200, "bytes=0-9", None),
        ("hls", f"{sid}/0/0.mp3", 200, None, None),
    ]
    assert entries[2]["bytes"] == len(segment.content)
    assert entries[1]["bytes"] == len(playlist.content)
