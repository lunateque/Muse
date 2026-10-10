from collections.abc import AsyncIterator, Callable

import anyio
import structlog

from muse.catalog.domain import (
    FILE_GONE,
    NOT_PLAYABLE,
    PAD_WARN_S,
    REENCODE_WAIT_S,
    RUN_IO_S,
    RUN_READ,
    RunCutError,
    RunRow,
    TrackStorage,
)
from muse.shared.mp3 import STREAM_KIND, FrameReader, Joiner

log = structlog.get_logger()


async def bounded[*A, T](fn: Callable[[*A], T], *args: *A, limit: float = RUN_IO_S) -> T | None:
    with anyio.move_on_after(limit):
        return await anyio.to_thread.run_sync(fn, *args, abandon_on_cancel=True)
    return None


class SongFrames:
    def __init__(self, storage: TrackStorage) -> None:
        self.storage = storage

    async def bodies(self, row: RunRow, joiner: Joiner) -> AsyncIterator[bytes]:
        if not joiner.left:
            return
        reader = FrameReader()
        async for body in self._native(row, joiner, reader):
            yield body
        if reader.kind != STREAM_KIND:
            encoded = await self._reencoded(row, joiner)
            if encoded:
                yield encoded
        missing = joiner.left * STREAM_KIND.samples / STREAM_KIND.rate
        if missing > PAD_WARN_S:
            log.warning("run padded", track=row["id"], seconds=round(missing, 1))
        if padding := joiner.finish():
            yield padding

    async def joined(self, row: RunRow, joiner: Joiner) -> bytes:
        return b"".join([body async for body in self.bodies(row, joiner)])

    async def _native(
        self, row: RunRow, joiner: Joiner, reader: FrameReader
    ) -> AsyncIterator[bytes]:
        source = await bounded(self.storage.open_audio, row["path"])
        if source is None:
            raise RunCutError(FILE_GONE)
        try:
            while joiner.left:
                try:
                    chunk = await bounded(source.read, RUN_READ)
                except OSError:
                    raise RunCutError("read failed") from None
                if chunk is None:
                    raise RunCutError("read timeout")
                frames = reader.push(chunk)
                if reader.kind is not None and reader.kind != STREAM_KIND:
                    return
                if body := joiner.take(frames):
                    yield body
                if not chunk or reader.done:
                    return
        finally:
            with anyio.CancelScope(shield=True):
                await bounded(source.close)

    async def _reencoded(self, row: RunRow, joiner: Joiner) -> bytes:
        data = await bounded(self.storage.reencoded, row["path"], limit=REENCODE_WAIT_S)
        if data is None:
            raise RunCutError(NOT_PLAYABLE)
        reader = FrameReader()
        frames = reader.push(data)
        if reader.kind != STREAM_KIND:
            raise RunCutError(NOT_PLAYABLE)
        return joiner.take(frames)
