import sqlite3
from contextlib import closing

import httpx

from muse.notifications.infra.sender import RecordingSender
from muse.settings import Settings
from tests.server.support import WRITE, ManualClock

type Client = httpx.AsyncClient


async def hide(client: Client, track_id: int, text: str) -> httpx.Response:
    return await client.post(
        "/api/secrets", headers=WRITE, json={"track_id": track_id, "text": text}
    )


async def test_a_hidden_secret_waits_for_the_partner_to_find_it(
    alice: Client, bob: Client, track_id: int, pushes: RecordingSender, clock: ManualClock
) -> None:
    track = (await alice.get(f"/api/track/{track_id}")).json()

    sent = await hide(bob, track_id, "  тайна  ")

    assert sent.status_code == 201, sent.text
    secret_id = sent.json()["id"]
    waiting = await alice.post(f"/api/secrets/find/{track_id}", headers=WRITE)
    found_at = clock.ms
    assert waiting.status_code == 200
    assert waiting.json() == {
        "secret": {
            "id": secret_id,
            "track": track,
            "text": "тайна",
            "created_at": clock.ms,
            "found_at": found_at,
        }
    }
    again = await alice.post(f"/api/secrets/find/{track_id}", headers=WRITE)
    assert again.status_code == 200
    assert again.json() == {"secret": None}

    [(user, title, body, url)] = pushes.sent
    assert (user, title, body, url) == (
        "bob",
        "🦊 Лисёнок: записка найдена",
        f"🎵 {track['title']} · {track['artist']}",
        "/us",
    )


async def test_a_secret_for_an_unknown_track_is_rejected(alice: Client) -> None:
    response = await hide(alice, 999_999, "hi")

    assert (response.status_code, response.json()) == (404, {"detail": "no track"})


async def test_hiding_again_on_the_same_track_replaces_the_waiting_text(
    alice: Client, bob: Client, track_id: int
) -> None:
    await hide(alice, track_id, "first")
    replaced = await hide(alice, track_id, "second")

    assert replaced.status_code == 201
    hidden = (await alice.get("/api/secrets")).json()["hidden"]
    assert [secret["text"] for secret in hidden] == ["second"]
    waiting = await bob.post(f"/api/secrets/find/{track_id}", headers=WRITE)
    assert waiting.json()["secret"]["text"] == "second"


async def test_the_listing_shows_what_i_hid_and_what_i_found_never_the_waiting_surprise(
    alice: Client, bob: Client, track_id: int, other_track_id: int, clock: ManualClock
) -> None:
    await hide(alice, track_id, "waiting")
    clock.ms += 1_000
    await hide(bob, other_track_id, "for alice")
    clock.ms += 1_000
    await hide(bob, other_track_id, "second one")
    clock.ms += 1_000
    await alice.post(f"/api/secrets/find/{other_track_id}", headers=WRITE)
    clock.ms += 1_000
    await hide(alice, other_track_id, "found by bob")
    clock.ms += 1_000
    await bob.post(f"/api/secrets/find/{other_track_id}", headers=WRITE)

    mine = (await alice.get("/api/secrets")).json()
    theirs = (await bob.get("/api/secrets")).json()

    assert [secret["text"] for secret in mine["hidden"]] == ["found by bob", "waiting"]
    assert [secret["text"] for secret in mine["found"]] == ["second one"]
    assert [secret["text"] for secret in theirs["found"]] == ["found by bob"]
    assert [secret["text"] for secret in theirs["hidden"]] == ["second one"]


async def test_only_the_sender_drops_a_waiting_secret(
    alice: Client, bob: Client, track_id: int
) -> None:
    secret_id = (await hide(alice, track_id, "still waiting")).json()["id"]

    assert (await bob.delete(f"/api/secrets/{secret_id}", headers=WRITE)).status_code == 404
    assert (await alice.delete("/api/secrets/999999", headers=WRITE)).status_code == 404
    assert (await alice.delete(f"/api/secrets/{secret_id}", headers=WRITE)).status_code == 204
    assert (await alice.get("/api/secrets")).json() == {"hidden": [], "found": []}


async def test_a_found_secret_cannot_be_dropped(alice: Client, bob: Client, track_id: int) -> None:
    secret_id = (await hide(alice, track_id, "to find")).json()["id"]
    await bob.post(f"/api/secrets/find/{track_id}", headers=WRITE)

    assert (await alice.delete(f"/api/secrets/{secret_id}", headers=WRITE)).status_code == 404
    assert [secret["text"] for secret in (await alice.get("/api/secrets")).json()["hidden"]] == [
        "to find"
    ]


async def test_a_letter_needs_its_text_first(alice: Client, track_id: int) -> None:
    unwritten = await hide(alice, 999_999, "   ")

    assert (unwritten.status_code, unwritten.json()) == (422, {"detail": "bad text"})


async def test_unauthenticated_requests_are_rejected(
    client: httpx.AsyncClient, track_id: int
) -> None:
    assert (await client.get("/api/secrets")).status_code == 401
    assert (
        await client.post("/api/secrets", headers=WRITE, json={"track_id": track_id, "text": "x"})
    ).status_code == 401
    assert (await client.post(f"/api/secrets/find/{track_id}", headers=WRITE)).status_code == 401
    assert (await client.delete("/api/secrets/1", headers=WRITE)).status_code == 401


async def test_secrets_replace_keeps_one_row_per_track(
    alice: Client, settings: Settings, track_id: int
) -> None:
    await hide(alice, track_id, "one")
    await hide(alice, track_id, "two")

    with closing(sqlite3.connect(settings.paths.user_db)) as db:
        rows = db.execute("SELECT text FROM secrets WHERE sender='alice'").fetchall()
    assert rows == [("two",)]
