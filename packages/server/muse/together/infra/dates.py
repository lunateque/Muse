import json
from pathlib import Path

import anyio
import structlog

from muse.together.dates import parse_dates

log = structlog.get_logger()


class DatesFile:
    def __init__(self, dates_file: Path) -> None:
        self.dates_file = dates_file

    async def load(self) -> object:
        return await anyio.to_thread.run_sync(self._read)

    def _read(self) -> object:
        if not self.dates_file.is_file():
            return {}
        try:
            document = json.loads(self.dates_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            log.warning(f"dates: {error}")
            return {}
        entries = document.get("dates") if isinstance(document, dict) else None
        if isinstance(entries, list) and (valid := len(parse_dates(document))) != len(entries):
            log.warning(f"dates: skipped {len(entries) - valid} invalid entries")
        return document
