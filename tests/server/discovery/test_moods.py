import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np
import pytest
from dishka import Provider, Scope, provide
from litestar import Litestar
from litestar.testing import AsyncTestClient

from muse.app import create_app
from muse.discovery.domain import TextEncoder, TextUnavailableError
from muse.discovery.service import VectorIndexes
from muse.settings import Settings
from tests.fixtures.catalog import FixtureCatalog
from tests.fixtures.vectors import MOODS
from tests.server.catalog.support import fixture_rows
from tests.server.conftest import with_catalog_copy
from tests.server.support import ORIGIN, WRITE, ManualClock, ManualClockProvider, signed_in

NOW_MS = 1_800_000_000_000
DAY_MS = 86_400_000
CYRILLIC = re.compile("[\u0400-\u04ff]")
MOOD_ID = "drive"


class FakeEncoder:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.asked: list[str] = []
        self.down = False

    async def embed(self, text: str) -> list[float]:
        self.asked.append(text)
        if self.down:
            raise TextUnavailableError("down")
        return self.vector


class EncoderProvider(Provider):
    def __init__(self, encoder: FakeEncoder) -> None:
        super().__init__()
        self._encoder = encoder

    @provide(scope=Scope.APP)
    def encoder(self) -> TextEncoder:
        return self._encoder


def mood_vector(library: FixtureCatalog, mood_id: str) -> list[float]:
    (row,) = fixture_rows(library, "SELECT vec FROM moods WHERE id=?", (mood_id,))
    return np.frombuffer(row[0], dtype="<f2").astype(float).tolist()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW_MS)


@pytest.fixture
def encoder(library: FixtureCatalog) -> FakeEncoder:
    return FakeEncoder(mood_vector(library, "dawn"))


@pytest.fixture
async def app(
    settings: Settings, clock: ManualClock, encoder: FakeEncoder
) -> AsyncIterator[Litestar]:
    providers = [ManualClockProvider(clock), EncoderProvider(encoder)]
    application = create_app(settings, providers=providers)
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        yield application


def genres_of(library: FixtureCatalog, tracks: list[dict[str, Any]]) -> Counter[str]:
    sql = "SELECT al.genre FROM tracks t JOIN albums al ON al.id=t.album_id WHERE t.id=?"
    return Counter(fixture_rows(library, sql, (item["id"],))[0][0] for item in tracks)


def sounds_russian(library: FixtureCatalog, track: dict[str, Any]) -> bool:
    genre = genres_of(library, [track])
    return bool(
        CYRILLIC.search(track["artist"])
        or CYRILLIC.search(track["title"])
        or next(iter(genre)).startswith("RU")
    )


async def test_the_mood_list_follows_the_file_with_a_mosaic_for_each_mood(
    alice: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    body = (await alice.get("/api/moods")).json()

    assert [group["title"] for group in body["groups"]] == ["Движ", "Утро", "Грусть", "Дорога"]
    cards = [mood for group in body["groups"] for mood in group["moods"]]
    assert [(card["id"], card["title"]) for card in cards] == [(row[0], row[1]) for row in MOODS]
    assert all(set(card) == {"id", "title", "albums"} for card in cards)
    assert all(len(set(card["albums"])) == len(card["albums"]) <= 4 for card in cards)
    assert (await client.get("/api/moods")).status_code == 401


async def test_mosaics_are_built_together_from_best_songs_and_share_no_album_more_than_twice(
    settings: Settings,
    library: FixtureCatalog,
    alice: httpx.AsyncClient,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*_: object) -> list[int]:
        raise AssertionError("a mosaic must not draw a mix")

    monkeypatch.setattr("muse.discovery.service.draw_mood", never)
    settings.paths.covers_dir.mkdir(parents=True, exist_ok=True)
    for (album_id,) in fixture_rows(library, "SELECT id FROM albums"):
        (settings.paths.covers_dir / f"{album_id}.jpg").write_bytes(library.cover_jpeg)

    def albums(body: dict[str, Any]) -> dict[str, list[int]]:
        cards = [card for group in body["groups"] for card in group["moods"]]
        return {card["id"]: card["albums"] for card in cards}

    today = albums((await alice.get("/api/moods")).json())
    clock.ms += DAY_MS
    tomorrow = albums((await alice.get("/api/moods")).json())
    shelves = (await alice.get("/api/home")).json()["moods"]

    assert list(today) == [row[0] for row in MOODS]
    assert all(len(set(shown)) == len(shown) == 4 for shown in today.values())
    assert max(Counter(album for shown in today.values() for album in shown).values()) <= 2
    assert tomorrow == today
    assert all(card["albums"] == today[card["id"]] for shelf in shelves for card in shelf["moods"])


async def test_a_mood_mix_is_one_draw_for_both_listeners_that_changes_each_day(
    library: FixtureCatalog,
    alice: httpx.AsyncClient,
    bob: httpx.AsyncClient,
    clock: ManualClock,
) -> None:
    first = (await alice.get(f"/api/mood/{MOOD_ID}")).json()
    shown = {item["id"] for item in first["tracks"]}

    assert (first["id"], first["title"], first["group"]) == (MOOD_ID, "Рэп в дорогу", "Движ")
    assert set(first) == {"id", "title", "group", "tracks", "albums"}
    assert len(first["tracks"]) == len(shown) == 200
    assert max(Counter(item["artist"] for item in first["tracks"]).values()) <= 3
    assert genres_of(library, first["tracks"])["Rap"] >= 80
    assert (await bob.get(f"/api/mood/{MOOD_ID}")).json() == first
    assert set(first["albums"]) <= {item["album_id"] for item in first["tracks"]}
    clock.ms += DAY_MS
    tomorrow = (await alice.get(f"/api/mood/{MOOD_ID}")).json()
    assert {item["id"] for item in tomorrow["tracks"]} != shown
    missing = await alice.get("/api/mood/нет-такого")
    assert (missing.status_code, missing.json()) == (404, {"detail": "no mood"})


async def test_a_mood_can_hold_to_one_vocal_side(
    library: FixtureCatalog, alice: httpx.AsyncClient
) -> None:
    russian = (await alice.get("/api/mood/tears")).json()["tracks"]
    foreign = (await alice.get("/api/mood/gym")).json()["tracks"]

    assert russian
    assert all(sounds_russian(library, item) for item in russian)
    assert foreign
    assert not any(sounds_russian(library, item) for item in foreign)


async def test_free_text_becomes_a_mix_by_the_sound_of_its_vector(
    library: FixtureCatalog,
    alice: httpx.AsyncClient,
    bob: httpx.AsyncClient,
    encoder: FakeEncoder,
) -> None:
    response = await alice.get("/api/mood-search", params={"q": "  Тихий   рассвет "})
    body = response.json()

    assert response.status_code == 200, response.text
    assert body["id"].startswith("q:")
    assert (body["title"], body["group"]) == ("Тихий рассвет", "Поиск")
    assert set(body) == {"id", "title", "group", "tracks", "albums"}
    assert len(body["tracks"]) == 200
    assert genres_of(library, body["tracks"])["Indie"] >= 60
    again = (await bob.get("/api/mood-search", params={"q": "тихий рассвет"})).json()
    assert again["id"] == body["id"]
    assert again["tracks"] == body["tracks"]
    assert encoder.asked == ["Тихий рассвет"]
    other = (await alice.get("/api/mood-search", params={"q": "something else"})).json()
    assert other["id"] != body["id"]
    assert other["tracks"] != body["tracks"]


async def test_mood_search_answers_503_while_the_text_service_or_its_vector_is_unusable(
    alice: httpx.AsyncClient, encoder: FakeEncoder
) -> None:
    encoder.down = True
    down = await alice.get("/api/mood-search", params={"q": "ночь"})
    encoder.down, encoder.vector = False, [1.0, 0.0]
    wrong = await alice.get("/api/mood-search", params={"q": "ночь"})

    assert (down.status_code, down.json()) == (503, {"detail": "mood search unavailable"})
    assert (wrong.status_code, wrong.json()) == (503, {"detail": "mood search unavailable"})


@pytest.mark.parametrize("query", ["", "   ", "x" * 121])
async def test_mood_search_wants_one_to_120_characters(
    alice: httpx.AsyncClient, query: str
) -> None:
    assert (await alice.get("/api/mood-search", params={"q": query})).status_code == 422
    assert (await alice.get("/api/mood-search")).status_code == 422


async def test_the_home_offers_three_mood_shelves_before_the_genre_row(
    alice: httpx.AsyncClient,
) -> None:
    body = (await alice.get("/api/home")).json()

    assert len(body["moods"]) == 3
    groups = {group["title"] for group in (await alice.get("/api/moods")).json()["groups"]}
    assert {shelf["title"] for shelf in body["moods"]} <= groups
    assert all(shelf["moods"] for shelf in body["moods"])
    assert all(set(card) == {"id", "title", "albums"} for card in body["moods"][0]["moods"])
    assert body["mixes"]
    assert all(card["title"].startswith("Микс · ") for card in body["mixes"])


async def test_radio_can_stay_in_a_mood_and_knows_which_moods_exist(
    library: FixtureCatalog, bob: httpx.AsyncClient
) -> None:
    seed = fixture_rows(library, "SELECT id FROM tracks WHERE status='ok' ORDER BY id")[0][0]

    moody = await bob.get(f"/api/radio/{seed}", params={"mood": "slow"})
    unknown = await bob.get(f"/api/radio/{seed}", params={"mood": "nope"})

    assert moody.status_code == 200, moody.text
    assert len(moody.json()["tracks"]) == 25
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "no mood"})


async def test_a_song_i_skipped_early_stays_out_of_my_radio_for_a_month(
    library: FixtureCatalog, bob: httpx.AsyncClient, clock: ManualClock
) -> None:
    seed = fixture_rows(library, "SELECT id FROM tracks WHERE status='ok' ORDER BY id")[0][0]
    params = {"n": 50}

    async def drawn() -> list[set[int]]:
        calls = [(await bob.get(f"/api/radio/{seed}", params=params)).json() for _ in range(8)]
        return [{item["id"] for item in call["tracks"]} for call in calls]

    liked = Counter(track_id for draw in await drawn() for track_id in draw)
    skipped_id, _ = liked.most_common(1)[0]
    report = {
        "track_id": skipped_id,
        "started_at": NOW_MS - 5000,
        "listened_ms": 5000,
        "completed": False,
        "skipped": True,
        "source": "radio",
    }
    assert (await bob.post("/api/plays", json=report, headers=WRITE)).status_code == 204
    clock.ms += DAY_MS

    assert all(skipped_id not in draw for draw in await drawn())
    clock.ms += 31 * DAY_MS
    assert any(skipped_id in draw for draw in await drawn())


async def test_without_the_vector_tables_the_old_catalog_still_works(
    legacy_settings: Settings, library: FixtureCatalog, clock: ManualClock, encoder: FakeEncoder
) -> None:
    providers = [ManualClockProvider(clock), EncoderProvider(encoder)]
    application = create_app(legacy_settings, providers=providers)
    seed = fixture_rows(library, "SELECT id FROM tracks WHERE status='ok' ORDER BY id")[0][0]
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        bob = await signed_in(application, "bob")
        moods = await bob.get("/api/moods")
        mood = await bob.get(f"/api/mood/{MOOD_ID}")
        search = await bob.get("/api/mood-search", params={"q": "ночь"})
        radio = await bob.get(f"/api/radio/{seed}")
        with_mood = await bob.get(f"/api/radio/{seed}", params={"mood": MOOD_ID})
        home = (await bob.get("/api/home")).json()
        async with application.state.dishka_container() as request:
            topup = await (await request.get(VectorIndexes)).topup()
        await bob.aclose()

    assert (moods.status_code, moods.json()) == (200, {"groups": []})
    assert (mood.status_code, mood.json()) == (404, {"detail": "no mood"})
    assert (search.status_code, search.json()) == (503, {"detail": "mood search unavailable"})
    assert radio.status_code == 200
    assert len(radio.json()["tracks"]) == 25
    assert (with_mood.status_code, with_mood.json()) == (404, {"detail": "no mood"})
    assert home["moods"] == []
    assert home["mixes"]
    assert topup is None
    assert encoder.asked == []


async def test_a_seed_without_a_vector_keeps_the_old_radio_even_inside_a_mood(
    settings: Settings, library: FixtureCatalog, tmp_path: Any, clock: ManualClock
) -> None:
    seed = fixture_rows(library, "SELECT id FROM tracks WHERE status='ok' ORDER BY id")[0][0]

    def forget(db: Any) -> None:
        db.execute("DELETE FROM track_vectors WHERE track_id=?", (seed,))

    partial = with_catalog_copy(settings, library.catalog_db, tmp_path / "partial.sqlite", forget)
    application = create_app(partial, providers=[ManualClockProvider(clock)])
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        bob = await signed_in(application, "bob")
        plain = await bob.get(f"/api/radio/{seed}")
        moody = await bob.get(f"/api/radio/{seed}", params={"mood": "slow"})
        await bob.aclose()

    assert len(plain.json()["tracks"]) == 25
    assert len(moody.json()["tracks"]) == 25
