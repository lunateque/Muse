import io

import pytest
from mutagen.id3 import ID3

from muse.hls.domain import SEG_FRAMES, SongAudio, SongPiece
from muse.hls.render import (
    has_segment,
    playlist_text,
    segment_body,
    segment_count,
    segment_length,
    segment_ticks,
    song_pieces,
    syncsafe,
    timestamp_tag,
)
from muse.shared.mp3 import STREAM_KIND
from tests.fixtures.audio import FRAME_LENGTH, mp3_bytes
from tests.server.hls.support import playlist_entries, split_tag, stamp

PER_SECOND = STREAM_KIND.rate / STREAM_KIND.samples
TICKS_PER_FRAME = 2160


def test_song_pieces_follow_the_joiner_rounding_and_skip_the_start() -> None:
    pieces = song_pieces([100.0, 50.5, 3.0], at=10.0)

    first = round(90.0 * PER_SECOND)
    second = round(140.5 * PER_SECOND) - first
    third = round(143.5 * PER_SECOND) - first - second
    assert pieces == [
        SongPiece(first, round(10.0 * PER_SECOND)),
        SongPiece(second, 0),
        SongPiece(third, 0),
    ]


def test_a_song_shorter_than_a_frame_adds_no_frames() -> None:
    assert song_pieces([10.0, 0.01], at=0.0)[1] == SongPiece(0, 0)


@pytest.mark.parametrize(
    ("frames", "segments", "last"),
    [
        (0, 0, 0),
        (1, 1, 1),
        (SEG_FRAMES - 1, 1, SEG_FRAMES - 1),
        (SEG_FRAMES, 1, SEG_FRAMES),
        (SEG_FRAMES + 1, 2, 1),
        (2 * SEG_FRAMES, 2, SEG_FRAMES),
        (2 * SEG_FRAMES + 168, 3, 168),
    ],
)
def test_a_song_is_cut_into_whole_segments_and_a_short_last_one(
    frames: int, segments: int, last: int
) -> None:
    assert segment_count(frames) == segments
    assert segments == 0 or segment_length(frames, segments - 1) == last
    assert all(segment_length(frames, index) == SEG_FRAMES for index in range(segments - 1))


def test_the_playlist_lists_every_segment_with_its_exact_length() -> None:
    pieces = song_pieces([100.0, 30.0], at=4.0)

    text = playlist_text("abc", pieces, ended=False)

    lines = text.splitlines()
    assert lines[:6] == [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:30",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
    ]
    entries = playlist_entries(text)
    assert len(entries) == sum(segment_count(piece.frames) for piece in pieces)
    assert entries[0] == ("29.952", "abc/0/0.mp3")
    assert entries[-1][1] == f"abc/1/{segment_count(pieces[1].frames) - 1}.mp3"
    assert abs(sum(float(length) for length, _ in entries) - 126.0) < 0.024
    assert "#EXT-X-ENDLIST" not in text
    assert text.endswith("\n")


def test_the_playlist_closes_only_when_the_session_ended() -> None:
    text = playlist_text("abc", [SongPiece(10, 0)], ended=True)

    assert text.splitlines()[-1] == "#EXT-X-ENDLIST"
    assert playlist_entries(text) == [("0.240", "abc/0/0.mp3")]


def test_an_empty_session_has_a_playlist_without_segments() -> None:
    assert playlist_entries(playlist_text("abc", [], ended=False)) == []


def test_segments_exist_only_inside_the_session() -> None:
    pieces = [SongPiece(3 * SEG_FRAMES, 0), SongPiece(0, 0), SongPiece(1, 0)]

    found = [(song, index) for song in range(-1, 4) for index in range(-1, 4)]

    assert [pair for pair in found if has_segment(pieces, *pair)] == [
        (0, 0),
        (0, 1),
        (0, 2),
        (2, 0),
    ]


def test_segment_timestamps_run_on_the_session_timeline_across_songs() -> None:
    pieces = [SongPiece(3 * SEG_FRAMES, 0), SongPiece(2 * SEG_FRAMES, 0)]

    assert segment_ticks(pieces, 0, 0) == 0
    assert segment_ticks(pieces, 0, 2) == 2 * SEG_FRAMES * TICKS_PER_FRAME
    assert segment_ticks(pieces, 1, 0) == 3 * SEG_FRAMES * TICKS_PER_FRAME
    assert segment_ticks(pieces, 1, 1) == 4 * SEG_FRAMES * TICKS_PER_FRAME


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "00000000"),
        (127, "0000007f"),
        (128, "00000100"),
        (0x3FFF, "00007f7f"),
        (0x4000, "00010000"),
        (0x0FFFFFFF, "7f7f7f7f"),
        (0x1234567, "090d0a67"),
    ],
)
def test_sizes_are_syncsafe(value: int, expected: str) -> None:
    assert syncsafe(value).hex() == expected


def test_the_tag_is_an_id3v24_priv_frame_with_the_big_endian_timestamp() -> None:
    ticks = 0x1_2345_6789

    tag = timestamp_tag(ticks)

    assert tag[:3] == b"ID3"
    assert tag[3:6] == b"\x04\x00\x00"
    assert tag[10:14] == b"PRIV"
    owner = b"com.apple.streaming.transportStreamTimestamp\x00"
    assert tag[10 + 10 :] == owner + ticks.to_bytes(8, "big")
    assert split_tag(tag + b"audio") == (tag, b"audio")
    parsed = ID3(fileobj=io.BytesIO(tag))
    assert parsed.version == (2, 4, 0)
    assert stamp(tag) == ticks


def test_the_timestamp_is_cut_to_33_bits() -> None:
    ticks = (1 << 33) + 5

    assert stamp(timestamp_tag(ticks)) == 5


def test_a_segment_is_the_tag_and_whole_frames_cut_on_frame_boundaries() -> None:
    total = 3 * SEG_FRAMES + 168
    audio = SongAudio.of(mp3_bytes(total))

    bodies = [
        segment_body(audio, index, index * SEG_FRAMES * TICKS_PER_FRAME) for index in range(4)
    ]

    split = [split_tag(body) for body in bodies]
    assert [len(frames) // FRAME_LENGTH for _, frames in split] == [
        SEG_FRAMES,
        SEG_FRAMES,
        SEG_FRAMES,
        168,
    ]
    assert b"".join(frames for _, frames in split) == mp3_bytes(total)
    stamps = [stamp(tag) for tag, _ in split]
    assert stamps == [
        0,
        SEG_FRAMES * TICKS_PER_FRAME,
        2 * SEG_FRAMES * TICKS_PER_FRAME,
        3 * SEG_FRAMES * TICKS_PER_FRAME,
    ]


def test_audio_offsets_ignore_a_truncated_frame_at_the_end() -> None:
    audio = SongAudio.of(mp3_bytes(3) + mp3_bytes(1)[:200])

    assert audio.frames == 3
    assert audio.offsets == (0, 384, 768, 1152)
    assert audio.slice(1, 2) == mp3_bytes(1)
