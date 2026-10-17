import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PULL = Path(__file__).resolve().parents[2] / "deploy" / "bin" / "muse-catalog-pull"
RCLONE = """#!/bin/sh
case "$1" in
  md5sum) echo "$(md5sum "$REMOTE" | cut -d' ' -f1)  catalog" ;;
  copyto)
    echo "rclone copyto" >> "$CALLS"
    cp "$REMOTE" "$3"
    [ ! -f "$FAIL/corrupt" ] || echo broken >> "$3" ;;
  *) exit 2 ;;
esac
"""
CURL = """#!/bin/sh
echo "curl $*" >> "$CALLS"
echo "login: $(cat)" >> "$CALLS"
echo "live while refreshing: $(cat "$DATA/state.sqlite")" >> "$CALLS"
[ ! -f "$FAIL/rc" ] || exit 22
if [ -f "$FAIL/rc-partial" ]; then echo '{"result": {"": "Failed: boom"}}'; else echo '{"result": {"": "OK"}}'; fi
"""
STUBS = {
    "rclone": RCLONE,
    "curl": CURL,
    "sqlite3": '[ -f "$FAIL/integrity" ] && echo corrupt || echo ok',
    "chown": "exit 0",
}
DAY_S = 24 * 3600

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell")


class Host:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.stamp = root / "stamp"
        self.remote = root / "remote.sqlite"
        self.calls = root / "calls"
        self.fail = root / "fail"

    def run(self, *failures: str) -> subprocess.CompletedProcess[str]:
        for flag in self.fail.iterdir():
            flag.unlink()
        for failure in failures:
            (self.fail / failure).touch()
        self.calls.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "MUSE_CATALOG_REMOTE": "storage:catalog/state.sqlite",
            "REMOTE": str(self.remote),
            "RCLONE_RC_USER": "muse-lib",
            "RCLONE_RC_PASS": "secret",
            "CALLS": str(self.calls),
            "FAIL": str(self.fail),
            "DATA": str(self.data),
        }
        return subprocess.run(
            ["sh", str(self.root / "pull")], env=env, capture_output=True, text=True, check=False
        )

    def calls_made(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def live(self) -> str:
        return (self.data / "state.sqlite").read_text(encoding="utf-8")

    def age_stamp(self, seconds: int) -> None:
        past = time.time() - seconds
        os.utime(self.stamp, (past, past))


@pytest.fixture
def host(tmp_path: Path) -> Host:
    result = Host(tmp_path)
    result.data.mkdir()
    result.fail.mkdir()
    (tmp_path / "bin").mkdir()
    for name, body in STUBS.items():
        stub = tmp_path / "bin" / name
        stub.write_text(body if body.startswith("#!") else f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    script = PULL.read_text(encoding="utf-8")
    script = script.replace("/srv/muse/data", str(result.data))
    script = script.replace("/var/lib/muse-lib-refreshed", str(result.stamp))
    (tmp_path / "pull").write_text(script, encoding="utf-8")
    (result.data / "state.sqlite").write_text("old", encoding="utf-8")
    result.remote.write_text("new", encoding="utf-8")
    return result


def test_a_new_catalog_refreshes_the_mount_before_it_goes_live(host: Host) -> None:
    done = host.run()

    assert done.returncode == 0
    assert host.calls_made() == [
        "rclone copyto",
        "curl -sS --fail --max-time 300 -K - -d recursive=true http://127.0.0.1:5572/vfs/refresh",
        'login: user = "muse-lib:secret"',
        "live while refreshing: old",
    ]
    assert host.live() == "new"
    assert host.stamp.exists()
    assert done.stdout.startswith("catalog: updated ")


def test_a_failed_refresh_is_reported_and_the_catalog_still_goes_live(host: Host) -> None:
    done = host.run("rc")

    assert done.returncode == 0
    assert done.stderr == "catalog: lib refresh failed\n"
    assert host.live() == "new"
    assert not host.stamp.exists()


def test_a_refresh_the_mount_reports_as_failed_is_not_a_success(host: Host) -> None:
    done = host.run("rc-partial")

    assert done.returncode == 0
    assert done.stderr == "catalog: lib refresh failed\n"
    assert host.live() == "new"
    assert not host.stamp.exists()


def test_a_corrupt_download_never_refreshes_or_replaces_the_catalog(host: Host) -> None:
    done = host.run("corrupt")

    assert done.returncode == 1
    assert done.stderr == "catalog: md5 mismatch\n"
    assert host.calls_made() == ["rclone copyto"]
    assert host.live() == "old"


def test_a_catalog_failing_the_integrity_check_never_refreshes_the_mount(host: Host) -> None:
    done = host.run("integrity")

    assert done.returncode == 1
    assert done.stderr == "catalog: integrity failed\n"
    assert host.calls_made() == ["rclone copyto"]
    assert host.live() == "old"


def test_an_unchanged_catalog_with_a_fresh_stamp_touches_nothing(host: Host) -> None:
    (host.data / "state.sqlite").write_text("new", encoding="utf-8")
    host.stamp.touch()
    host.age_stamp(DAY_S - 600)

    done = host.run()

    assert done.returncode == 0
    assert host.calls_made() == []
    assert done.stdout == ""


def test_an_unchanged_catalog_with_a_day_old_stamp_refreshes_the_mount(host: Host) -> None:
    (host.data / "state.sqlite").write_text("new", encoding="utf-8")
    host.stamp.touch()
    host.age_stamp(DAY_S + 600)

    done = host.run()

    assert done.returncode == 0
    assert host.calls_made()[0].endswith("http://127.0.0.1:5572/vfs/refresh")
    assert time.time() - host.stamp.stat().st_mtime < 60


def test_an_unchanged_catalog_without_a_stamp_refreshes_the_mount(host: Host) -> None:
    (host.data / "state.sqlite").write_text("new", encoding="utf-8")

    done = host.run()

    assert done.returncode == 0
    assert len(host.calls_made()) == 3
    assert host.stamp.exists()


def test_a_failed_periodic_refresh_drops_the_stamp(host: Host) -> None:
    (host.data / "state.sqlite").write_text("new", encoding="utf-8")
    host.stamp.touch()
    host.age_stamp(DAY_S + 600)

    done = host.run("rc")

    assert done.returncode == 0
    assert done.stderr == "catalog: lib refresh failed\n"
    assert not host.stamp.exists()


def test_a_failed_refresh_on_a_new_catalog_is_retried_on_the_next_run(host: Host) -> None:
    host.stamp.touch()
    first = host.run("rc")

    second = host.run()

    assert first.stderr == "catalog: lib refresh failed\n"
    assert second.returncode == 0
    assert host.calls_made()[0].endswith("http://127.0.0.1:5572/vfs/refresh")
    assert host.stamp.exists()
