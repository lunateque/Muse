import os
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "deploy" / "bin" / "muse-catalog-install"
STUBS = {"chown": "exit 0"}
INTEGRITY_OK = "ok"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="needs a POSIX shell")


def make_db(path: Path, rows: int) -> None:
    subprocess.run(
        [
            "sqlite3",
            str(path),
            "CREATE TABLE t(x); " + "".join(f"INSERT INTO t VALUES ({n}); " for n in range(rows)),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


class Host:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.incoming = root / "incoming" / "state.sqlite"
        self.dst = root / "data" / "state.sqlite"

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"}
        return subprocess.run(
            ["sh", str(self.root / "install")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def check_integrity(self) -> str:
        done = subprocess.run(
            ["sqlite3", "-readonly", str(self.dst), "PRAGMA integrity_check;"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return done.stdout.strip()


@pytest.fixture
def host(tmp_path: Path) -> Host:
    result = Host(tmp_path)
    result.incoming.parent.mkdir()
    result.dst.parent.mkdir()
    (tmp_path / "bin").mkdir()
    for name, body in STUBS.items():
        stub = tmp_path / "bin" / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    script = INSTALL.read_text(encoding="utf-8")
    script = script.replace("/var/lib/muse-incoming/state.sqlite", str(result.incoming))
    script = script.replace("/srv/muse/data/state.sqlite", str(result.dst))
    (tmp_path / "install").write_text(script, encoding="utf-8")
    make_db(result.dst, 2)
    return result


def test_no_incoming_file_changes_nothing(host: Host) -> None:
    before = host.dst.read_bytes()
    done = host.run()
    assert done.returncode == 0
    assert done.stdout == ""
    assert host.dst.read_bytes() == before


def test_an_unchanged_incoming_copy_is_dropped(host: Host) -> None:
    host.incoming.write_bytes(host.dst.read_bytes())
    done = host.run()
    assert done.returncode == 0
    assert done.stdout.startswith("catalog: incoming unchanged")
    assert not host.incoming.exists()


def test_a_fresh_catalog_is_installed_and_the_incoming_slot_freed(host: Host) -> None:
    make_db(host.incoming, 3)
    fresh = host.incoming.read_bytes()
    done = host.run()
    assert done.returncode == 0
    assert done.stdout.startswith("catalog: installed ")
    assert host.dst.read_bytes() == fresh
    assert not host.incoming.exists()
    assert host.check_integrity() == INTEGRITY_OK


def test_a_file_failing_the_integrity_check_never_replaces_the_live_catalog(host: Host) -> None:
    before = host.dst.read_bytes()
    host.incoming.write_bytes(b"this is not a database" * 100)
    done = host.run()
    assert done.returncode == 1
    assert not host.incoming.exists()
    assert host.dst.read_bytes() == before
