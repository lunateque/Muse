import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import closing
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
if [ "$(md5sum < "$DATA/state.sqlite")" = "$(md5sum < "$REMOTE")" ]; then live=new; else live=old; fi
echo "live: $live" >> "$CALLS"
dir=/
for arg in "$@"; do case $arg in dir=*) dir=${arg#dir=} ;; esac; done
listed() { grep -qxF "$dir" "$FAIL/$1" 2>/dev/null; }
cost=$(cat "$FAIL/elapse" 2>/dev/null || echo 0)
if listed hang; then cost=60; fi
echo $(($(cat "$CLOCK") + cost * 1000)) > "$CLOCK"
[ ! -f "$FAIL/rc" ] || exit 7
if listed hang; then echo "curl: (28) Operation timed out" >&2; exit 28; fi
case " $* " in *" _async=true "*) echo '{"jobid": 1}'; exit 0 ;; esac
if listed partial; then echo "{\\"result\\": {\\"$dir\\": \\"Failed: boom\\"}}"; exit 0; fi
if listed missing; then echo "{\\"result\\": {\\"$dir\\": \\"file does not exist\\"}}"; exit 0; fi
echo "{\\"result\\": {\\"$dir\\": \\"OK\\"}}"
"""
STUBS = {
    "rclone": RCLONE,
    "curl": CURL,
    "date": 'cat "$CLOCK"',
    "chown": "exit 0",
}
CLOCK_START_MS = 1_800_000_000_000
RUN_LIMIT_S = 60
LOGIN = 'login: user = "muse-lib:secret"'
RC_URL = "http://127.0.0.1:5572/vfs/refresh"
Row = tuple[str, str, int, int]
OLD = [
    ("Indie/X/Album1/a.mp3", "ok", 10, 100),
    ("Indie/X/Album1/b.mp3", "ok", 20, 200),
    ("Indie/Y/Album2/c.mp3", "ok", 30, 300),
    ("Rock/Z/Album3/d.mp3", "ok", 40, 400),
    ("Rock/Z/Album3/e.mp3", "ok", 50, 500),
    ("Rock/Q/Album5/f.mp3", "fail", 60, 600),
]
NEW = [
    ("Indie/X/Album1/a.mp3", "ok", 10, 100),
    ("Indie/X/Album1/b.mp3", "ok", 20, 200),
    ("Indie/X/Album4/new.mp3", "ok", 70, 700),
    ("Rock/Z/Album3/d.mp3", "ok", 40, 401),
    ("Rock/Z/Album3/e.mp3", "ok", 50, 500),
    ("Rock/Q/Album5/f.mp3", "ok", 60, 600),
]
CHANGED = [
    "Indie",
    "Rock",
    "Indie/X",
    "Indie/Y",
    "Rock/Q",
    "Rock/Z",
    "Indie/X/Album4",
    "Indie/Y/Album2",
    "Rock/Q/Album5",
    "Rock/Z/Album3",
]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell")


def make_catalog(path: Path, rows: Sequence[Row], *, identity: bool = True) -> None:
    path.unlink(missing_ok=True)
    columns = "id INTEGER PRIMARY KEY, path TEXT, status TEXT"
    if identity:
        columns += ", size INTEGER, mtime_ns INTEGER"
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"CREATE TABLE tracks({columns})")
        for path_, status, size, mtime in rows:
            if identity:
                db.execute(
                    "INSERT INTO tracks(path, status, size, mtime_ns) VALUES (?,?,?,?)",
                    (path_, status, size, mtime),
                )
            else:
                db.execute("INSERT INTO tracks(path, status) VALUES (?,?)", (path_, status))
        db.commit()


class Host:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.pending = root / "pending"
        self.backstop = root / "backstop"
        self.remote = root / "remote.sqlite"
        self.calls = root / "calls"
        self.fail = root / "fail"
        self.clock = root / "clock"

    def run(
        self,
        *failures: str,
        elapse: int = 0,
        hang: Sequence[str] = (),
        partial: Sequence[str] = (),
        missing: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        for flag in self.fail.iterdir():
            flag.unlink()
        for failure in failures:
            (self.fail / failure).touch()
        (self.fail / "elapse").write_text(str(elapse), encoding="utf-8")
        for name, dirs in (("hang", hang), ("partial", partial), ("missing", missing)):
            (self.fail / name).write_text("".join(f"{d}\n" for d in dirs), encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        self.clock.write_text(str(CLOCK_START_MS), encoding="utf-8")
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
            "CLOCK": str(self.clock),
        }
        return subprocess.run(
            ["sh", str(self.root / "pull")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_LIMIT_S,
        )

    def calls_made(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def rc_calls(self) -> list[str]:
        return [line for line in self.calls_made() if line.startswith("curl ")]

    def refreshed(self) -> list[str]:
        found = []
        for line in self.rc_calls():
            if "_async=true" in line:
                found.append("async")
            elif match := re.search(r"--data-urlencode dir=(.*) -d recursive=false ", line):
                found.append(match.group(1))
            else:
                found.append("/")
        return found

    def live_states(self) -> list[str]:
        return [line[6:] for line in self.calls_made() if line.startswith("live: ")]

    def is_live(self) -> bool:
        return (self.data / "state.sqlite").read_bytes() == self.remote.read_bytes()

    def pending_dirs(self) -> list[str]:
        if not self.pending.exists():
            return []
        return self.pending.read_text(encoding="utf-8").splitlines()

    def become_current(self) -> None:
        (self.data / "state.sqlite").write_bytes(self.remote.read_bytes())

    def age_backstop(self, seconds: int) -> None:
        past = time.time() - seconds
        os.utime(self.backstop, (past, past))


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
    script = script.replace("/var/lib/muse-lib-pending", str(result.pending))
    script = script.replace("/var/lib/muse-lib-backstop", str(result.backstop))
    (tmp_path / "pull").write_text(script, encoding="utf-8")
    make_catalog(result.data / "state.sqlite", OLD)
    make_catalog(result.remote, NEW)
    result.backstop.touch()
    return result


def test_a_new_catalog_refreshes_the_ancestor_dirs_of_new_removed_and_replaced_files_before_it_goes_live(
    host: Host,
) -> None:
    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == CHANGED
    assert host.live_states() == ["old"] * len(CHANGED)
    assert host.calls_made()[:5] == [
        "rclone copyto",
        f"curl -sS --fail --max-time 60 -K - --data-urlencode dir=Indie -d recursive=false {RC_URL}",
        LOGIN,
        "live: old",
        f"curl -sS --fail --max-time 60 -K - --data-urlencode dir=Rock -d recursive=false {RC_URL}",
    ]
    assert host.is_live()
    assert host.pending_dirs() == []
    assert done.stdout.splitlines()[0] == "catalog: refreshed Indie in 0 ms"
    assert done.stdout.splitlines()[-1].startswith("catalog: updated ")


def test_every_refresh_call_reports_how_long_it_took(host: Host) -> None:
    done = host.run(elapse=3)

    assert done.stdout.splitlines()[:2] == [
        "catalog: refreshed Indie in 3000 ms",
        "catalog: refreshed Rock in 3000 ms",
    ]


def test_an_old_catalog_without_identity_columns_falls_back_to_path_differences(
    host: Host,
) -> None:
    make_catalog(host.data / "state.sqlite", OLD, identity=False)

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == [
        "Indie",
        "Rock",
        "Indie/X",
        "Indie/Y",
        "Rock/Q",
        "Indie/X/Album4",
        "Indie/Y/Album2",
        "Rock/Q/Album5",
    ]
    assert host.is_live()


def test_comparing_full_size_catalogs_takes_seconds_not_minutes(host: Host) -> None:
    rows = [(f"G{n % 20}/A{n % 700}/B{n % 3000}/{n}.mp3", "ok", n, n) for n in range(40_000)]
    changed = ("G14/A534/B1234/1234.mp3", "ok", 1234, -1)
    make_catalog(host.data / "state.sqlite", rows)
    make_catalog(host.remote, [*rows[:1234], changed, *rows[1235:]])

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == ["G14", "G14/A534", "G14/A534/B1234"]
    assert host.is_live()


def test_a_new_catalog_without_identity_columns_falls_back_to_path_differences(
    host: Host,
) -> None:
    make_catalog(host.remote, NEW, identity=False)

    done = host.run()

    assert done.returncode == 0
    assert "Rock/Z/Album3" not in host.refreshed()
    assert "Indie/X/Album4" in host.refreshed()
    assert host.is_live()


def test_paths_of_an_older_catalog_with_backslashes_match_the_same_files(host: Host) -> None:
    make_catalog(
        host.data / "state.sqlite",
        [(path.replace("/", "\\"), status, size, mtime) for path, status, size, mtime in NEW],
    )

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == []
    assert host.is_live()


def test_a_genre_new_to_the_mount_refreshes_the_library_root_first(host: Host) -> None:
    make_catalog(host.remote, [*NEW, ("Jazz Hop/Артист & Co/Альбом/т.mp3", "ok", 1, 1)])

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == [
        "/",
        "Indie",
        "Jazz Hop",
        "Rock",
        "Indie/X",
        "Indie/Y",
        "Jazz Hop/Артист & Co",
        "Rock/Q",
        "Rock/Z",
        "Indie/X/Album4",
        "Indie/Y/Album2",
        "Jazz Hop/Артист & Co/Альбом",
        "Rock/Q/Album5",
        "Rock/Z/Album3",
    ]
    assert host.rc_calls()[0] == f"curl -sS --fail --max-time 60 -K - -d recursive=false {RC_URL}"


def test_a_dir_that_is_gone_from_the_bucket_is_done_not_pending(host: Host) -> None:
    done = host.run(missing=["Indie/Y/Album2"])

    assert done.returncode == 0
    assert host.pending_dirs() == []
    assert "Indie/Y/Album2" in host.refreshed()


def test_a_timed_out_dir_goes_to_pending_with_the_dirs_below_it_and_the_catalog_goes_live(
    host: Host,
) -> None:
    done = host.run(hang=["Indie/X"])

    assert done.returncode == 0
    assert host.refreshed() == [d for d in CHANGED if d != "Indie/X/Album4"]
    assert host.pending_dirs() == ["Indie/X", "Indie/X/Album4"]
    assert "catalog: refresh of Indie/X failed after 60000 ms" in done.stderr
    assert host.is_live()


def test_pending_dirs_are_retried_first_by_the_next_run_with_an_unchanged_catalog(
    host: Host,
) -> None:
    host.run(hang=["Indie/X"])

    second = host.run()

    assert second.returncode == 0
    assert host.refreshed() == ["Indie/X", "Indie/X/Album4"]
    assert host.pending_dirs() == []
    assert second.stdout.splitlines() == [
        "catalog: refreshed Indie/X in 0 ms",
        "catalog: refreshed Indie/X/Album4 in 0 ms",
    ]


def test_a_pending_dir_that_fails_again_stays_pending(host: Host) -> None:
    host.run(hang=["Indie/X"])

    second = host.run(hang=["Indie/X"])

    assert second.returncode == 0
    assert host.refreshed() == ["Indie/X"]
    assert host.pending_dirs() == ["Indie/X", "Indie/X/Album4"]


def test_pending_dirs_go_ahead_of_the_dirs_of_the_next_catalog(host: Host) -> None:
    host.run(hang=["Rock/Z/Album3"])
    make_catalog(host.remote, [*NEW, ("Pop/P/Album6/p.mp3", "ok", 1, 1)])

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed()[0] == "Rock/Z/Album3"
    assert host.refreshed()[1:3] == ["/", "Pop"]
    assert host.pending_dirs() == []


def test_a_partial_refresh_the_mount_reports_as_failed_goes_to_pending(host: Host) -> None:
    done = host.run(partial=["Rock"])

    assert done.returncode == 0
    assert host.pending_dirs() == ["Rock", "Rock/Q", "Rock/Z", "Rock/Q/Album5", "Rock/Z/Album3"]
    assert host.is_live()


def test_an_unreachable_mount_leaves_every_dir_pending_and_the_catalog_still_goes_live(
    host: Host,
) -> None:
    done = host.run("rc")

    assert done.returncode == 0
    assert host.refreshed() == ["Indie", "Rock"]
    assert host.pending_dirs() == CHANGED
    assert host.is_live()


def test_the_budget_stops_new_calls_and_the_catalog_still_goes_live(host: Host) -> None:
    done = host.run(elapse=100)

    assert done.returncode == 0
    assert host.refreshed() == CHANGED[:3]
    assert host.pending_dirs() == CHANGED[3:]
    assert host.live_states() == ["old"] * 3
    assert "catalog: refresh budget of 300000 ms used up" in done.stderr
    assert host.is_live()
    assert done.stdout.splitlines()[-1].startswith("catalog: updated ")


def test_the_budget_also_bounds_the_retry_of_pending_dirs(host: Host) -> None:
    host.run(elapse=100)

    done = host.run(elapse=100)

    assert done.returncode == 0
    assert host.refreshed() == CHANGED[3:6]
    assert host.pending_dirs() == CHANGED[6:]


def test_an_unchanged_catalog_with_no_pending_dirs_makes_no_rc_call(host: Host) -> None:
    host.become_current()

    done = host.run()

    assert done.returncode == 0
    assert host.calls_made() == []
    assert done.stdout == ""


def test_the_backstop_is_one_async_recursive_refresh_at_most_daily(host: Host) -> None:
    host.become_current()
    host.age_backstop(25 * 3600)

    first = host.run()
    first_calls = host.refreshed()
    second = host.run()

    assert first.returncode == 0
    assert first_calls == ["async"]
    assert first.stdout == "catalog: full refresh started in the background\n"
    assert host.calls_made() == []
    assert second.stdout == ""


def test_the_backstop_call_is_async_recursive_and_short(host: Host) -> None:
    host.become_current()
    host.backstop.unlink()

    host.run()

    assert host.calls_made() == [
        f"curl -sS --fail --max-time 15 -K - -d recursive=true -d _async=true {RC_URL}",
        LOGIN,
        "live: new",
    ]
    assert time.time() - host.backstop.stat().st_mtime < 60


def test_a_backstop_younger_than_a_day_is_left_alone(host: Host) -> None:
    host.become_current()
    host.age_backstop(23 * 3600)

    host.run()

    assert host.calls_made() == []


def test_a_failing_backstop_never_fails_the_pull_and_is_tried_again_next_time(host: Host) -> None:
    host.become_current()
    host.backstop.unlink()

    done = host.run("rc")

    assert done.returncode == 0
    assert done.stderr.endswith("catalog: background refresh not started\n")
    assert not host.backstop.exists()
    assert host.run().stdout == "catalog: full refresh started in the background\n"


def test_the_backstop_runs_after_the_catalog_went_live(host: Host) -> None:
    host.backstop.unlink()

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed()[-1] == "async"
    assert host.live_states()[-1] == "new"
    assert done.stdout.splitlines()[-2].startswith("catalog: updated ")


def test_a_live_catalog_that_cannot_be_read_still_goes_live_with_a_full_background_refresh(
    host: Host,
) -> None:
    (host.data / "state.sqlite").write_text("garbage", encoding="utf-8")

    done = host.run()

    assert done.returncode == 0
    assert host.refreshed() == ["async"]
    assert host.live_states() == ["new"]
    assert "cannot compare with the live catalog" in done.stderr
    assert host.is_live()


def test_a_corrupt_download_never_refreshes_or_replaces_the_catalog(host: Host) -> None:
    before = (host.data / "state.sqlite").read_bytes()

    done = host.run("corrupt")

    assert done.returncode == 1
    assert done.stderr == "catalog: md5 mismatch\n"
    assert host.calls_made() == ["rclone copyto"]
    assert (host.data / "state.sqlite").read_bytes() == before


def test_a_catalog_failing_the_integrity_check_never_refreshes_the_mount(host: Host) -> None:
    host.remote.write_bytes(b"this is not a database" * 100)
    before = (host.data / "state.sqlite").read_bytes()

    done = host.run()

    assert done.returncode == 1
    assert "catalog: integrity failed" in done.stderr
    assert host.calls_made() == ["rclone copyto"]
    assert (host.data / "state.sqlite").read_bytes() == before
