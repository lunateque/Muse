import sqlite3
from contextlib import closing

import httpx

from muse.settings import Settings
from tests.server.support import WRITE, ManualClock

type Client = httpx.AsyncClient


async def test_a_pin_for_today_is_seen_by_the_partner_and_replaces(
    alice: Client, bob: Client, track_id: int, other_track_id: int, pushes, clock: ManualClock
) -> None:
    track = (await alice.get(f"/api/track/{track_id}")).json()
    other = (await alice.get(f"/api/track/{other_track_id}")).json()

    first = await alice.put(
        "/api/pin", headers=WRITE, json={"track_id": track_id, "text": "  утро  "}
    )
    assert first.status_code == 204
    [(user, title, body, url)] = pushes.sent
    assert (user, title, body, url) == (
        "bob",
        "🦊 Лисёнок: песня дня для тебя",
        f"🎵 {track['title']} · {track['artist']}",
        "/",
    )
    pushes.sent.clear()
    second = await alice.put(
        "/api/pin", headers=WRITE, json={"track_id": other_track_id, "text": ""}
    )
    assert second.status_code == 204
    [(_, title, body, _)] = pushes.sent
    assert (title, body) == (
        "🦊 Лисёнок: песня дня для тебя",
        f"🎵 {other['title']} · {other['artist']}",
    )

    theirs = (await bob.get("/api/pin")).json()
    assert theirs["for_me"] == {"track": other, "text": "", "set_at": clock.ms}
    assert theirs["from_me"] is None

    mine = (await alice.get("/api/pin")).json()
    assert mine["from_me"] == {"track": other, "text": "", "set_at": clock.ms}
    assert mine["for_me"] is None


async def test_pin_views_are_isolated_per_user_and_day(
    alice: Client, bob: Client, track_id: int, clock: ManualClock
) -> None:
    await alice.put("/api/pin", headers=WRITE, json={"track_id": track_id})
    clock.ms += 86_400_000

    alice_next_day = (await alice.get("/api/pin")).json()
    assert alice_next_day == {"for_me": None, "from_me": None}


async def test_pin_caption_is_limited(alice: Client, track_id: int) -> None:
    assert (
        await alice.put("/api/pin", headers=WRITE, json={"track_id": track_id, "text": "x" * 141})
    ).status_code == 422
    assert (
        await alice.put("/api/pin", headers=WRITE, json={"track_id": track_id, "text": "x" * 140})
    ).status_code == 204
    assert (
        await alice.put("/api/pin", headers=WRITE, json={"track_id": 999_999})
    ).status_code == 404


async def test_unpin_removes_only_my_pin_for_today(
    alice: Client, bob: Client, track_id: int, settings: Settings, clock: ManualClock
) -> None:
    day = clock.today().isoformat()
    await alice.put("/api/pin", headers=WRITE, json={"track_id": track_id, "text": "для боба"})
    await bob.put("/api/pin", headers=WRITE, json={"track_id": track_id, "text": "для алисы"})

    assert (await alice.delete("/api/pin", headers=WRITE)).status_code == 204

    with closing(sqlite3.connect(settings.paths.user_db)) as db:
        rows = db.execute("SELECT for_user, set_by FROM pins WHERE day=?", (day,)).fetchall()
    assert rows == [("alice", "bob")]
    view = (await bob.get("/api/pin")).json()
    assert view["for_me"] is None
    assert view["from_me"]["text"] == "для алисы"


async def test_unauthenticated_pin_requests_are_rejected(
    client: httpx.AsyncClient, track_id: int
) -> None:
    assert (await client.get("/api/pin")).status_code == 401
    assert (
        await client.put("/api/pin", headers=WRITE, json={"track_id": track_id})
    ).status_code == 401
    assert (await client.delete("/api/pin", headers=WRITE)).status_code == 401
