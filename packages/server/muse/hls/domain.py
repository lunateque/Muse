import re
from dataclasses import dataclass
from typing import Protocol, TypedDict

from muse.shared.mp3 import frame_at

SEG_FRAMES = 1248
TARGET_DURATION_S = 30
MAX_SONGS = 500
MAX_APPEND = 200
SESSION_TTL_MS = 2 * 24 * 3600 * 1000
SONG_CACHE = 8
TICKS_PER_SECOND = 90000
TIMESTAMP_BITS = 33
TIMESTAMP_OWNER = b"com.apple.streaming.transportStreamTimestamp"
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{22,64}")
PLAYLIST_NAME = re.compile(r"([A-Za-z0-9_-]{22,64})\.m3u8")
SEGMENT_NAME = re.compile(r"(\d{1,6})\.mp3")
PLAYLIST_WAIT_S = 5.0
PLAYLIST_POLL_S = 0.1
NO_SESSION = "no session"
NO_SEGMENT = "no segment"
BAD_SESSION_ID = "bad session id"
SEGMENT_FAILED = "segment unavailable"


class SongInfo(TypedDict):
    id: int
    dur: int


class NewSession(TypedDict):
    id: str
    url: str
    songs: list[SongInfo]


class Appended(TypedDict):
    songs: list[SongInfo]


@dataclass(frozen=True, slots=True)
class HlsSession:
    id: str
    user: str
    ids: list[int]
    at: float
    ended: bool


@dataclass(frozen=True, slots=True)
class SongPiece:
    frames: int
    skip: int


@dataclass(frozen=True, slots=True)
class SongAudio:
    data: bytes
    offsets: tuple[int, ...]

    @classmethod
    def of(cls, data: bytes) -> SongAudio:
        offsets = [0]
        while (found := frame_at(data, offsets[-1])) is not None:
            if offsets[-1] + found[1] > len(data):
                break
            offsets.append(offsets[-1] + found[1])
        return cls(data, tuple(offsets))

    @property
    def frames(self) -> int:
        return len(self.offsets) - 1

    def slice(self, first: int, last: int) -> bytes:
        return self.data[self.offsets[first] : self.offsets[last]]


class HlsStore(Protocol):
    async def create(self, session: HlsSession, now: int) -> bool: ...

    async def get(self, session_id: str) -> HlsSession | None: ...

    async def save(self, session_id: str, ids: list[int], ended: bool, now: int) -> None: ...

    async def purge(self, before: int) -> None: ...
