from datetime import date

import pytest

from muse.shared.errors import DomainError
from muse.together.dates import (
    parse_dates,
    todays,
    together_days,
    together_since,
)
from muse.together.domain import (
    pin_text,
    valid_note,
)


def test_pin_text_strips_and_limits() -> None:
    assert pin_text("  привет  ") == "привет"
    assert pin_text("x" * 140) == "x" * 140
    with pytest.raises(DomainError):
        pin_text("x" * 141)


def test_song_notes_need_an_integer_track_id() -> None:
    assert valid_note({"kind": "song", "text": "x", "track_id": 7})
    assert not valid_note({"kind": "song", "text": "x"})
    assert not valid_note({"kind": "song", "text": "x", "track_id": "7"})
    assert not valid_note({"kind": "song", "text": "x", "track_id": 7.5})
    assert not valid_note({"kind": "song", "text": "x", "track_id": True})
    assert not valid_note({"kind": "fact", "text": "x", "track_id": 7})
    assert valid_note({"kind": "fact", "text": "x"})


def test_parse_dates_keeps_month_day_and_full_dates() -> None:
    entries = parse_dates(
        {
            "dates": [
                {"date": "01-17", "title": "день"},
                {"date": "2024-01-17", "title": "год"},
                {"date": "13-45", "title": "битая"},
                {"date": "2024-1-7", "title": "короткий"},
                {"title": "без даты"},
                {"date": "01-17"},
                {"date": "01-17", "title": "   "},
                "строка",
            ]
        }
    )

    assert [(entry.date, entry.title, entry.full) for entry in entries] == [
        ("01-17", "день", False),
        ("2024-01-17", "год", True),
    ]


def test_parse_dates_without_a_list_is_empty() -> None:
    assert parse_dates({}) == []
    assert parse_dates({"dates": "x"}) == []
    assert parse_dates(None) == []
    assert parse_dates([{"date": "01-17", "title": "x"}]) == []


def test_together_since_accepts_only_full_dates() -> None:
    assert together_since({"together_since": "2024-01-17"}) == "2024-01-17"
    assert together_since({"together_since": "01-17"}) is None
    assert together_since({"together_since": None}) is None
    assert together_since({}) is None


def test_together_days_counts_the_first_day_as_one() -> None:
    assert together_days("2024-01-17", date(2024, 1, 17)) == 1
    assert together_days("2024-01-17", date(2024, 1, 18)) == 2
    assert together_days("2024-01-17", date(2023, 1, 18)) is None
    assert together_days("не дата", date(2024, 1, 18)) is None
    assert together_days("2024-02-30", date(2024, 3, 1)) is None


def test_todays_matches_month_day_and_counts_years() -> None:
    entries = parse_dates(
        {
            "dates": [
                {"date": "01-17", "title": "день"},
                {"date": "2019-01-17", "title": "год"},
                {"date": "01-18", "title": "завтра"},
            ]
        }
    )

    assert todays(entries, date(2026, 1, 17)) == [
        {"title": "день", "years": None},
        {"title": "год", "years": 7},
    ]
    assert todays(entries, date(2026, 2, 1)) == []
