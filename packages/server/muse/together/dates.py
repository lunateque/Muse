import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol, TypedDict

MONTH_DAY = re.compile(r"\d{2}-\d{2}")
FULL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class TodayEntry(TypedDict):
    title: str
    years: int | None


class DatesView(TypedDict):
    together_days: int | None
    today: list[TodayEntry]


class DatesSource(Protocol):
    async def load(self) -> object: ...


@dataclass(frozen=True, slots=True)
class DateEntry:
    date: str
    title: str
    full: bool


def real_date(value: str, full: bool) -> bool:
    year, month, day = (2000, value[:2], value[3:5]) if not full else value.split("-")
    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True


def parse_dates(document: object) -> list[DateEntry]:
    entries = document.get("dates") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    parsed = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value, title = entry.get("date"), entry.get("title")
        full = bool(FULL_DATE.fullmatch(str(value or "")))
        shaped = full or bool(MONTH_DAY.fullmatch(str(value or "")))
        if shaped and real_date(str(value), full) and isinstance(title, str) and title.strip():
            parsed.append(DateEntry(value, title, full))
    return parsed


def together_since(document: object) -> str | None:
    value = document.get("together_since") if isinstance(document, dict) else None
    return value if isinstance(value, str) and FULL_DATE.fullmatch(value) else None


def together_days(since: str, today: date) -> int | None:
    try:
        start = date.fromisoformat(since)
    except ValueError:
        return None
    return (today - start).days + 1 if start <= today else None


def todays(entries: list[DateEntry], today: date) -> list[TodayEntry]:
    found: list[TodayEntry] = []
    stamp = today.strftime("%m-%d")
    for entry in entries:
        month_day = entry.date[-5:]
        if month_day != stamp:
            continue
        years = None
        if entry.full:
            years = today.year - int(entry.date[:4])
        found.append({"title": entry.title, "years": years})
    return found
