import struct
from collections.abc import Sequence

from muse.hls.domain import (
    SEG_FRAMES,
    TARGET_DURATION_S,
    TICKS_PER_SECOND,
    TIMESTAMP_BITS,
    TIMESTAMP_OWNER,
    SongAudio,
    SongPiece,
)
from muse.shared.mp3 import STREAM_KIND, Joiner

TICKS_PER_FRAME = STREAM_KIND.samples * TICKS_PER_SECOND // STREAM_KIND.rate
SYNCSAFE_SHIFTS = (21, 14, 7, 0)
ID3_VERSION = b"\x04\x00"
NO_FLAGS = b"\x00"
FRAME_FLAGS = b"\x00\x00"


def song_pieces(durations: Sequence[float], at: float) -> list[SongPiece]:
    joiner = Joiner(STREAM_KIND)
    pieces = []
    for index, seconds in enumerate(durations):
        joiner.song(seconds, at if index == 0 else 0.0)
        pieces.append(SongPiece(joiner.left, joiner.skip))
    return pieces


def segment_count(frames: int) -> int:
    return -(-frames // SEG_FRAMES)


def segment_length(frames: int, index: int) -> int:
    return min(SEG_FRAMES, frames - index * SEG_FRAMES)


def has_segment(pieces: Sequence[SongPiece], song: int, index: int) -> bool:
    return 0 <= song < len(pieces) and 0 <= index < segment_count(pieces[song].frames)


def extinf(frames: int) -> str:
    return f"{frames * STREAM_KIND.samples / STREAM_KIND.rate:.3f}"


def playlist_text(session_id: str, pieces: Sequence[SongPiece], *, ended: bool) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{TARGET_DURATION_S}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
    ]
    for song, piece in enumerate(pieces):
        for index in range(segment_count(piece.frames)):
            lines.append(f"#EXTINF:{extinf(segment_length(piece.frames, index))},")
            lines.append(f"{session_id}/{song}/{index}.mp3")
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def segment_ticks(pieces: Sequence[SongPiece], song: int, index: int) -> int:
    before = sum(piece.frames for piece in pieces[:song]) + index * SEG_FRAMES
    return before * TICKS_PER_FRAME


def syncsafe(value: int) -> bytes:
    return bytes(value >> shift & 0x7F for shift in SYNCSAFE_SHIFTS)


def timestamp_tag(ticks: int) -> bytes:
    stamp = struct.pack(">Q", ticks % (1 << TIMESTAMP_BITS))
    body = TIMESTAMP_OWNER + b"\x00" + stamp
    frame = b"PRIV" + syncsafe(len(body)) + FRAME_FLAGS + body
    return b"ID3" + ID3_VERSION + NO_FLAGS + syncsafe(len(frame)) + frame


def segment_body(audio: SongAudio, index: int, ticks: int) -> bytes:
    first = index * SEG_FRAMES
    return timestamp_tag(ticks) + audio.slice(first, min(first + SEG_FRAMES, audio.frames))
