import re
from collections.abc import AsyncIterator
from email.utils import formatdate, mktime_tz, parsedate_tz
from pathlib import Path

import anyio
from litestar import Request, Response
from litestar.response import Stream

CHUNK = 64 * 1024
SPEC = re.compile(r"\s*(\d*)\s*-\s*(\d*)\s*", re.ASCII)


class RangeNotSatisfiableError(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    unit, separator, spec = header.partition("=")
    if not separator or unit.strip().lower() != "bytes" or "," in spec:
        return None
    match = SPEC.fullmatch(spec)
    if match is None or not (match[1] or match[2]):
        return None
    if not match[1]:
        length = int(match[2])
        if length == 0 or size == 0:
            raise RangeNotSatisfiableError
        return max(0, size - length), size - 1
    start = int(match[1])
    end = int(match[2]) if match[2] else size - 1
    if match[2] and end < start:
        return None
    if start >= size:
        raise RangeNotSatisfiableError
    return start, min(end, size - 1)


async def chunks(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as file:
        await file.seek(start)
        remaining = length
        while remaining > 0:
            data = await file.read(min(CHUNK, remaining))
            if not data:
                return
            remaining -= len(data)
            yield data


def etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    if header.strip() == "*":
        return True
    return any(item.strip().removeprefix("W/") == etag for item in header.split(","))


def unmodified_since(header: str | None, modified: int) -> bool:
    parsed = parsedate_tz(header) if header else None
    if parsed is None:
        return False
    try:
        return modified <= mktime_tz(parsed)
    except OverflowError, ValueError:
        return False


def is_unchanged(request: Request, etag: str, modified: int) -> bool:
    known = request.headers.get("if-none-match")
    if known is not None:
        return etag_matches(known, etag)
    return unmodified_since(request.headers.get("if-modified-since"), modified)


async def file_response(path: Path, media_type: str, request: Request) -> Response[bytes] | Stream:
    stat = await anyio.Path(path).stat()
    size = stat.st_size
    etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
    modified = formatdate(stat.st_mtime, usegmt=True)
    headers = {"Accept-Ranges": "bytes", "Last-Modified": modified, "ETag": etag}
    if is_unchanged(request, etag, int(stat.st_mtime)):
        return Response(b"", status_code=304, media_type=media_type, headers=headers)
    requested = request.headers.get("range")
    validator = request.headers.get("if-range")
    if validator is not None and validator not in (etag, modified):
        requested = None
    try:
        span = parse_range(requested, size)
    except RangeNotSatisfiableError:
        headers["Content-Range"] = f"bytes */{size}"
        return Response(b"", status_code=416, media_type=media_type, headers=headers)
    start, end = span or (0, size - 1)
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = end - start + 1 if size else 0
    headers["Content-Length"] = str(length)
    return Stream(
        chunks(path, start, length),
        status_code=206 if span else 200,
        media_type=media_type,
        headers=headers,
    )
