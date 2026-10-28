# SPDX-License-Identifier: Apache-2.0

import io
from pathlib import Path

import pytest

from src.composefs import escape, registration, volume


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Two store paths, and an environment that links into one of them, the
    way buildEnv does."""
    store = tmp_path / "store"
    lib = store / "aaa-lib"
    (lib / "lib").mkdir(parents=True)
    (lib / "lib" / "libx.so").write_bytes(b"\x7fELF")
    (lib / "lib" / "libx.so.1").symlink_to("libx.so")
    app = store / "bbb-app"
    (app / "bin").mkdir(parents=True)
    (app / "bin" / "app").write_text("#!/bin/sh\necho hi\n")
    (app / "bin" / "app").chmod(0o555)
    (app / "etc").mkdir()
    (app / "etc" / "app.conf").write_text("a = b\n")
    (app / "lib").symlink_to(lib / "lib")
    (app / "share").symlink_to("/etc/outside")
    db = store / "ccc-image" / "db"
    db.mkdir(parents=True)
    (db / "db.sqlite").write_bytes(b"SQLite format 3\x00")
    return store


def dump(store: Path, primary: str | None = "bbb-app", db: bool = True) -> list[list[str]]:
    out = io.StringIO()
    volume(
        out,
        [store / "aaa-lib", store / "bbb-app"],
        store / primary if primary else None,
        store / "ccc-image" / "db" if db else None,
        store=store,
    )
    return [line.split(" ") for line in out.getvalue().splitlines()]


def by_path(lines: list[list[str]]) -> dict[str, list[str]]:
    return {fields[0]: fields for fields in lines}


def test_every_line_has_the_eleven_fields(store: Path) -> None:
    assert all(len(fields) == 11 for fields in dump(store))


def test_a_directory_comes_before_what_is_in_it(store: Path) -> None:
    written: set[str] = set()
    for fields in dump(store):
        parent = fields[0].rsplit("/", 1)[0] or "/"
        assert fields[0] == "/" or parent in written, f"{fields[0]} before {parent}"
        written.add(fields[0])


def test_a_store_file_is_backed_by_its_path_in_the_store(store: Path) -> None:
    lines = by_path(dump(store))
    app = lines["/nix/store/bbb-app/bin/app"]
    assert app[8] == "bbb-app/bin/app"
    assert int(app[1]) == (store / "bbb-app/bin/app").stat().st_size
    assert app[2] == "100555"


def test_a_symlink_in_the_store_stays_a_symlink(store: Path) -> None:
    link = by_path(dump(store))["/nix/store/aaa-lib/lib/libx.so.1"]
    assert link[2] == "120777"
    assert link[8] == "libx.so"
    assert link[1] == str(len("libx.so"))


def test_the_primary_tree_at_the_root_is_real_files(store: Path) -> None:
    lines = by_path(dump(store))
    # Followed into the other store path: a file, backed by where it lives.
    assert lines["/lib/libx.so"][2].startswith("100")
    assert lines["/lib/libx.so"][8] == "aaa-lib/lib/libx.so"
    assert lines["/etc/app.conf"][8] == "bbb-app/etc/app.conf"
    # A link that leaves the store stays a link: there is nothing to back it.
    assert lines["/share"][2] == "120777"
    assert lines["/share"][8] == "/etc/outside"


def test_result_and_database(store: Path) -> None:
    lines = by_path(dump(store))
    assert lines["/nix/var/result"][8] == escape(str(store / "bbb-app"))
    assert lines["/nix/var/nix/db/db.sqlite"][8] == "ccc-image/db/db.sqlite"


def test_without_a_primary_or_a_database(store: Path) -> None:
    paths = {fields[0] for fields in dump(store, primary=None, db=False)}
    assert "/nix/var/result" not in paths
    assert not any(p.startswith("/nix/var/nix") for p in paths)
    assert "/bin" not in paths


def test_the_same_closure_writes_the_same_dump(store: Path) -> None:
    assert dump(store) == dump(store)


def test_a_primary_with_a_nix_of_its_own_is_refused(store: Path) -> None:
    (store / "bbb-app" / "nix").mkdir()
    with pytest.raises(ValueError, match=r"twice|/nix of its own"):
        dump(store)


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("plain", "plain"),
        ("with space", "with\\x20space"),
        ("back\\slash", "back\\x5cslash"),
        ("a=b", "a\\x3db"),
        ("ünïcode", "\\xc3\\xbcn\\xc3\\xafcode"),
    ],
)
def test_escape(text: str, escaped: str) -> None:
    assert escape(text) == escaped


def test_registration_is_what_closure_info_writes() -> None:
    closure = [
        {
            "path": "/nix/store/aaa-lib",
            "narHash": "sha256-a",
            "narSize": 10,
            "references": [],
        },
        {
            "path": "/nix/store/bbb-app",
            "narHash": "sha256-b",
            "narSize": 20,
            "references": ["/nix/store/aaa-lib", "/nix/store/bbb-app"],
        },
    ]
    assert registration(closure).splitlines() == [
        "/nix/store/aaa-lib",
        "sha256-a",
        "10",
        "",
        "0",
        "/nix/store/bbb-app",
        "sha256-b",
        "20",
        "",
        "2",
        "/nix/store/aaa-lib",
        "/nix/store/bbb-app",
    ]
