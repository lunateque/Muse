import copy
import json
import sqlite3
import time
from typing import Any

import httpx
import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from muse.app import create_app
from muse.settings import Settings
from tests.server.support import ORIGIN, PASSWORDS, WRITE, browser, signed_in


def flags(cookie: str) -> set[str]:
    return {part.strip().lower() for part in cookie.split(";")}


async def test_only_the_session_cookie_authenticates(client: httpx.AsyncClient) -> None:
    anonymous = await client.get("/api/me")
    bearer = await client.get("/api/me", headers={"Authorization": "Bearer x"})

    for response in (anonymous, bearer):
        assert (response.status_code, response.json()) == (401, {"detail": "unauthorized"})


async def test_logins_are_rate_limited_and_the_cookie_is_locked_down(app: Litestar) -> None:
    async with browser(app) as client:
        attacker = {**WRITE, "CF-Connecting-IP": "1.2.3.4"}
        bad = {"login": "bob", "password": "wrong-password"}
        for _ in range(5):
            response = await client.post("/api/login", json=bad, headers=attacker)
            assert (response.status_code, response.json()) == (401, {"detail": "bad credentials"})
        blocked = await client.post("/api/login", json=bad, headers=attacker)

        response = await client.post(
            "/api/login",
            json={"login": " BOB ", "password": PASSWORDS["bob"]},
            headers={**WRITE, "CF-Connecting-IP": "5.6.7.8"},
        )

    assert (blocked.status_code, blocked.json()) == (429, {"detail": "too many attempts"})
    assert int(blocked.headers["retry-after"]) > 0
    assert response.status_code == 200
    assert response.json()["partner"]["id"] == "alice"
    assert {
        "httponly",
        "secure",
        "samesite=lax",
        "path=/",
        "max-age=15552000",
    } <= flags(response.headers["set-cookie"])
    assert response.headers["set-cookie"].startswith("muse_sid=")


async def test_me_and_logout(alice: httpx.AsyncClient) -> None:
    me = (await alice.get("/api/me")).json()
    assert (me["theme"], me["animal"], me["partner"]["id"]) == ("light", "fox", "bob")

    response = await alice.post("/api/logout", headers=WRITE)

    assert response.status_code == 204
    assert "max-age=0" in flags(response.headers["set-cookie"])
    assert (await alice.get("/api/me")).status_code == 401


async def test_an_idle_session_gets_its_cookie_renewed(
    alice: httpx.AsyncClient, settings: Settings
) -> None:
    token = alice.cookies["muse_sid"]
    two_days_ago = int(time.time()) - 2 * 86400
    connection = sqlite3.connect(settings.paths.user_db)
    connection.execute("UPDATE sessions SET last_seen=?", (two_days_ago,))
    connection.commit()
    connection.close()

    renewed = await alice.get("/api/me")
    again = await alice.get("/api/me")

    assert renewed.headers["set-cookie"].startswith(f"muse_sid={token};")
    assert "set-cookie" not in again.headers


async def test_a_malformed_login_is_a_422_list(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/login", json={"login": "alice"}, headers=WRITE)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("cover_colors", [True, False])
async def test_me_reports_the_cover_colors_flag(
    cover_colors: bool, settings: Settings, users_document: dict[str, Any]
) -> None:
    document = copy.deepcopy(users_document)
    if cover_colors:
        document["users"][0]["cover_colors"] = True
    (settings.paths.users_file).write_text(json.dumps(document), encoding="utf-8")

    app = create_app(settings)
    async with AsyncTestClient(app=app, base_url=ORIGIN):
        alice = await signed_in(app, "alice")
        me = (await alice.get("/api/me")).json()
        await alice.aclose()

    assert me["cover_colors"] is cover_colors
    assert "cover_colors" not in me["partner"]


@pytest.mark.parametrize("hls", [True, False])
async def test_me_reports_the_hls_flag(
    hls: bool, settings: Settings, users_document: dict[str, Any]
) -> None:
    document = copy.deepcopy(users_document)
    if hls:
        document["users"][0]["hls"] = True
    (settings.paths.users_file).write_text(json.dumps(document), encoding="utf-8")

    app = create_app(settings)
    async with AsyncTestClient(app=app, base_url=ORIGIN):
        alice = await signed_in(app, "alice")
        me = (await alice.get("/api/me")).json()
        await alice.aclose()

    assert me["hls"] is hls
    assert "hls" not in me["partner"]
