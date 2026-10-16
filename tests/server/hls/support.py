import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from litestar.testing import AsyncTestClient
from mutagen.id3 import ID3

from muse.app import create_app
from muse.catalog.domain import LibraryRoot
from muse.catalog.infra.files import TrackFiles
from muse.settings import Settings
from tests.fixtures.catalog import FixtureCatalog
from tests.server.catalog.support import fixture_rows
from tests.server.support import ORIGIN, WRITE, signed_in


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


def session_id(number: int = 0) -> str:
    return f"{number:022d}"


def shortest(
    library: FixtureCatalog, count: int, *, portable: bool = False
) -> list[tuple[int, int, Path]]:
    files = TrackFiles(library.library_dir, LibraryRoot.at(library.library_dir))
    sql = "SELECT id, dur, path FROM tracks WHERE status='ok' AND dur > 0 ORDER BY dur, id"
    found = [
        (row["id"], row["dur"], path)
        for row in fixture_rows(library, sql)
        if (path := files.locate(row["path"])) is not None
        and not (portable and Path(row["path"]).is_absolute())
    ]
    return found[:count]


async def open_session(
    client: httpx.AsyncClient, sid: str, ids: list[int], at: float = 0.0
) -> httpx.Response:
    return await client.put(f"/api/hls/{sid}", json={"ids": ids, "at": at}, headers=WRITE)


def playlist_entries(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    return [
        (lines[number].removeprefix("#EXTINF:").removesuffix(","), lines[number + 1])
        for number, line in enumerate(lines)
        if line.startswith("#EXTINF:")
    ]


def split_tag(segment: bytes) -> tuple[bytes, bytes]:
    size = segment[6] << 21 | segment[7] << 14 | segment[8] << 7 | segment[9]
    return segment[: 10 + size], segment[10 + size :]


def stamp(tag: bytes) -> int:
    [frame] = ID3(fileobj=io.BytesIO(tag)).getall("PRIV")
    assert frame.owner == "com.apple.streaming.transportStreamTimestamp"
    assert len(frame.data) == 8
    return int.from_bytes(frame.data, "big")
