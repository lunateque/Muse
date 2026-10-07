import json

import httpx
import pytest
from structlog.testing import capture_logs

from muse.settings import Settings
from tests.server.support import ManualClock

type Client = httpx.AsyncClient

DOCUMENT = {
    "together_since": "2026-01-01",
    "dates": [
        {"date": "01-17", "title": "зимний день"},
        {"date": "2024-01-17", "title": "год знакомства"},
        {"date": "13-45", "title": "битая дата"},
        {"date": "02-30", "title": "нет такого дня"},
        {"title": "без даты"},
        {"date": "01-17"},
        "строка",
    ],
}


async def test_dates_count_days_and_todays_entries(
    alice: Client, settings: Settings, clock: ManualClock
) -> None:
    settings.paths.dates_file.write_text(json.dumps(DOCUMENT), encoding="utf-8")

    view = (await alice.get("/api/dates")).json()

    assert view == {
        "together_days": 17,
        "today": [
            {"title": "зимний день", "years": None},
            {"title": "год знакомства", "years": 2},
        ],
    }


async def test_together_days_counts_the_starting_day_as_one(
    alice: Client, settings: Settings, clock: ManualClock
) -> None:
    settings.paths.dates_file.write_text(
        json.dumps({"together_since": "2026-01-17", "dates": []}), encoding="utf-8"
    )

    assert (await alice.get("/api/dates")).json()["together_days"] == 1

    clock.ms += 86_400_000 * 10
    assert (await alice.get("/api/dates")).json()["together_days"] == 11


async def test_a_future_together_since_and_a_broken_date_are_null(
    alice: Client, settings: Settings, clock: ManualClock
) -> None:
    settings.paths.dates_file.write_text(
        json.dumps({"together_since": "2027-01-01", "dates": []}), encoding="utf-8"
    )

    assert (await alice.get("/api/dates")).json()["together_days"] is None

    settings.paths.dates_file.write_text(
        json.dumps({"together_since": "некогда", "dates": []}), encoding="utf-8"
    )
    assert (await alice.get("/api/dates")).json()["together_days"] is None


async def test_an_absent_dates_file_is_an_empty_view(alice: Client, settings: Settings) -> None:
    assert (await alice.get("/api/dates")).json() == {"together_days": None, "today": []}


async def test_a_broken_dates_file_counts_as_empty_and_warns(
    alice: Client, settings: Settings
) -> None:
    settings.paths.dates_file.write_text("{broken", encoding="utf-8")

    with capture_logs() as logs:
        view = (await alice.get("/api/dates")).json()

    assert view == {"together_days": None, "today": []}
    assert logs[0]["event"].startswith("dates: ")


async def test_invalid_entries_are_skipped_with_a_warning(
    alice: Client, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    settings.paths.dates_file.write_text(
        json.dumps({"dates": [{"date": "xx", "title": "t"}]}), encoding="utf-8"
    )

    with capture_logs() as logs:
        view = (await alice.get("/api/dates")).json()

    assert view["today"] == []
    assert logs[0]["event"].startswith("dates: skipped 1 invalid entries")


async def test_dates_need_a_signed_in_user(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/dates")).status_code == 401
