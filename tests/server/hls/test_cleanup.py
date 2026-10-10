from collections.abc import AsyncIterator

import httpx
import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from muse.app import create_app
from muse.settings import Settings
from tests.fixtures.catalog import FixtureCatalog
from tests.server.hls.support import open_session, session_id, shortest
from tests.server.support import ORIGIN, WRITE, ManualClock, ManualClockProvider, signed_in

DAY_MS = 24 * 3600 * 1000
NOW_MS = 1_790_000_000_000


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW_MS)


@pytest.fixture
async def app(settings: Settings, clock: ManualClock) -> AsyncIterator[Litestar]:
    application = create_app(settings, providers=[ManualClockProvider(clock)])
    async with AsyncTestClient(app=application, base_url=ORIGIN):
        yield application


async def exists(client: httpx.AsyncClient, sid: str) -> bool:
    return (await client.get(f"/api/hls/{sid}/0/0.mp3")).status_code == 200


async def test_a_session_untouched_for_two_days_goes_when_a_new_one_is_created(
    app: Litestar, clock: ManualClock, library: FixtureCatalog
) -> None:
    [(track, _, _)] = shortest(library, 1)
    alice = await signed_in(app, "alice")
    await open_session(alice, session_id(1), [track])

    clock.ms = NOW_MS + 2 * DAY_MS
    await open_session(alice, session_id(2), [track])
    kept = await exists(alice, session_id(1))
    clock.ms = NOW_MS + 2 * DAY_MS + 1
    await open_session(alice, session_id(3), [track])

    assert kept
    assert not await exists(alice, session_id(1))
    assert await exists(alice, session_id(2))
    await alice.aclose()


async def test_appending_keeps_a_session_alive(
    app: Litestar, clock: ManualClock, library: FixtureCatalog
) -> None:
    [(track, _, _)] = shortest(library, 1)
    alice = await signed_in(app, "alice")
    await open_session(alice, session_id(4), [track])

    clock.ms = NOW_MS + 3 * DAY_MS // 2
    await alice.post(f"/api/hls/{session_id(4)}/append", json={"ids": [track]}, headers=WRITE)
    clock.ms = NOW_MS + 3 * DAY_MS
    await open_session(alice, session_id(5), [track])

    assert await exists(alice, session_id(4))
    await alice.aclose()
