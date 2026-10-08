import json
from collections import Counter
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from muse.app import create_app
from muse.catalog.domain import LibraryTrack
from muse.daily.domain import Ask, Listening, daily_pool
from muse.daily.service import DailyMaker
from muse.discovery.service import RadioModels
from muse.identity.domain import Users
from muse.settings import Settings
from tests.fixtures.catalog import FixtureCatalog
from tests.server.catalog.support import fixture_rows
from tests.server.support import ORIGIN, ManualClock, ManualClockProvider, signed_in

NOW_MS = 1_800_000_000_000
DAY = date(2026, 1, 15)


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[Litestar]:
    application = create_app(settings, providers=[ManualClockProvider(ManualClock(NOW_MS))])
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        yield application


async def first_pool(app: Litestar) -> list[LibraryTrack]:
    container = app.state.dishka_container
    users = await container.get(Users)
    model = await (await container.get(RadioModels)).current()
    nobody = Listening({u.id: [] for u in users.all}, {u.id: {} for u in users.all}, set())
    return daily_pool(DAY, model, users.all, nobody)


async def make(app: Litestar, day: date, ask: Ask) -> bool:
    async with app.state.dishka_container() as request:
        maker = await request.get(DailyMaker)
        return await maker.make(day, ask)


def shown(listing: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["for"], item["title"]) for item in listing["playlists"]]


async def test_daily_playlists_keep_only_real_tracks_from_the_pool(
    app: Litestar, alice: httpx.AsyncClient, bob: httpx.AsyncClient
) -> None:
    seen: set[str] = set()
    picks: list[int] = []
    for item in await first_pool(app):
        if item["artist"] not in seen:
            seen.add(item["artist"])
            picks.append(item["id"])
    assert len(picks) >= 60
    reply = json.dumps(
        {
            "playlists": [
                {
                    "for": "alice",
                    "title": "Утро",
                    "blurb": "бодро",
                    "tracks": [*picks[:20], 999999999, "x", picks[0]],
                },
                {"for": "Боб", "title": "Вечер", "blurb": "тихо", "tracks": picks[20:40]},
                {"for": "both", "title": "Вдвоём", "blurb": "", "tracks": picks[40:54]},
                {"for": "both", "title": "вдвоём", "tracks": picks[:20]},
                {"for": "alice", "title": "Мало", "tracks": picks[54:60]},
            ]
        },
        ensure_ascii=False,
    )
    prompts: list[str] = []

    async def ask(prompt: str) -> AsyncIterator[tuple[str, str]]:
        prompts.append(prompt)
        yield "broken", "не JSON"
        yield "test-model", f"```json\n{reply}\n```"

    assert await make(app, DAY, ask)

    assert f"\n{picks[0]}|" in prompts[0]
    mine = (await alice.get("/api/daily")).json()
    assert mine["day"] == DAY.isoformat()
    assert shown(mine) == [("alice", "Утро"), ("both", "Вдвоём"), ("bob", "Вечер")]
    assert shown((await bob.get("/api/daily")).json()) == [
        ("bob", "Вечер"),
        ("both", "Вдвоём"),
        ("alice", "Утро"),
    ]
    lists = {
        item["title"]: (await alice.get(f"/api/daily/{item['id']}")).json()
        for item in mine["playlists"]
    }
    assert [item["id"] for item in lists["Утро"]["tracks"]][:20] == picks[:20]
    assert len(lists["Вдвоём"]["tracks"]) > 14
    for listing in lists.values():
        ids = [item["id"] for item in listing["tracks"]]
        assert len(ids) == len(set(ids)) == 100
        assert max(Counter(item["artist"] for item in listing["tracks"]).values()) <= 3
    assert {item["id"]: item["tracks"] for item in mine["playlists"]} == {
        listing["id"]: len(listing["tracks"]) for listing in lists.values()
    }

    async def unusable(_prompt: str) -> AsyncIterator[tuple[str, str]]:
        yield "m", '{"playlists": [{"title": "x", "tracks": [1, 2]}]}'

    assert not await make(app, date(2026, 1, 16), unusable)
    assert (await bob.get("/api/daily")).json()["day"] == DAY.isoformat()
    missing = await alice.get("/api/daily/999999")
    assert (missing.status_code, missing.json()) == (404, {"detail": "no playlist"})


async def test_without_any_set_the_daily_listing_is_empty(alice: httpx.AsyncClient) -> None:
    assert (await alice.get("/api/daily")).json() == {"day": None, "playlists": []}


def indie_picks(pool: list[LibraryTrack], count: int) -> list[int]:
    seen: set[str] = set()
    picks: list[int] = []
    for item in pool:
        if item["genre"] == "Indie" and item["artist"] not in seen:
            seen.add(item["artist"])
            picks.append(item["id"])
    assert len(picks) >= count
    return picks[:count]


def three_playlists(picks: list[int]) -> Ask:
    reply = json.dumps(
        {"playlists": [{"for": "both", "title": title, "tracks": picks} for title in "АБВ"]}
    )

    async def ask(_prompt: str) -> AsyncIterator[tuple[str, str]]:
        yield "test-model", reply

    return ask


async def only_playlist(app: Litestar, alice: httpx.AsyncClient) -> list[dict[str, Any]]:
    card = (await alice.get("/api/daily")).json()["playlists"][0]
    return (await alice.get(f"/api/daily/{card['id']}")).json()["tracks"]


async def test_sound_tops_a_playlist_up_with_songs_that_sound_like_its_picks(
    app: Litestar, alice: httpx.AsyncClient, library: FixtureCatalog
) -> None:
    picks = indie_picks(await first_pool(app), 12)

    assert await make(app, DAY, three_playlists(picks))

    tracks = await only_playlist(app, alice)
    assert [item["id"] for item in tracks[:12]] == picks
    assert len({item["id"] for item in tracks}) == len(tracks) == 100
    assert max(Counter(item["artist"] for item in tracks).values()) <= 3
    genre = "SELECT al.genre FROM albums al JOIN tracks t ON t.album_id=al.id WHERE t.id=?"
    indie = [fixture_rows(library, genre, (item["id"],))[0][0] == "Indie" for item in tracks]
    assert sum(indie) >= 60


async def test_without_vectors_the_top_up_follows_artist_links_as_before(
    legacy_settings: Settings,
) -> None:
    application = create_app(legacy_settings, providers=[ManualClockProvider(ManualClock(NOW_MS))])
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        alice = await signed_in(application, "alice")
        picks = indie_picks(await first_pool(application), 12)

        assert await make(application, DAY, three_playlists(picks))

        tracks = await only_playlist(application, alice)
        await alice.aclose()
    assert [item["id"] for item in tracks[:12]] == picks
    assert 12 < len({item["id"] for item in tracks}) == len(tracks) <= 100
    assert max(Counter(item["artist"] for item in tracks).values()) <= 3
