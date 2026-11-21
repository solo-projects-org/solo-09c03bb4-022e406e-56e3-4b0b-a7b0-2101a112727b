"""Build throwaway archives that exercise the chain-switching paths.

Used by ``tools/selftest.sh`` so the behaviour can be demonstrated against a
real HTTP server, not only unit tests.  Never writes inside ``sample_archive``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def copy_sample(destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ROOT / "sample_archive", destination)
    return destination


def break_missing_patch(destination: Path) -> None:
    """Delete the cheapest October patch: the resolver must switch chains."""

    (destination / "patches/patch-09-to-10.json").unlink()


def break_every_route(destination: Path) -> None:
    """Remove the newer base and every package: no chain can succeed."""

    (destination / "tables/table-2026-09.json").unlink()
    for patch in (destination / "patches").glob("*.json"):
        patch.unlink()


def corrupt_both_bases(destination: Path) -> None:
    """Make every base table parseable but with the wrong digest."""

    for name in ("table-2026-06.json", "table-2026-09.json"):
        path = destination / "tables" / name
        table = json.loads(path.read_text(encoding="utf-8"))
        first_key = min(table)
        table[first_key]["price_fen"] += 1
        path.write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def corrupt_base_same_size_same_mtime(destination: Path) -> None:
    """Replace the June base with valid JSON of the same size and mtime.

    Only a fresh read and digest check can catch this; file metadata is
    unchanged, which is exactly the cache-consistency bug this guards.
    """

    path = destination / "tables/table-2026-06.json"
    stat = path.stat()
    table = json.loads(path.read_text(encoding="utf-8"))
    for key in sorted(table):
        value = table[key]["price_fen"]
        if value >= 100:
            table[key]["price_fen"] = value - 1 if value % 10 else value + 1
            break
    payload = (json.dumps(table, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) != stat.st_size:
        raise SystemExit("replacement changed the byte count; sample drifted")
    path.write_bytes(payload)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    if (path.stat().st_mtime_ns, path.stat().st_size) != (
        stat.st_mtime_ns,
        stat.st_size,
    ):
        raise SystemExit("replacement changed the metadata; sample drifted")


def drop_newer_base(destination: Path) -> None:
    """Leave a single base table, so a swap cannot fall back to another chain."""

    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["base_tables"] = [
        entry for entry in manifest["base_tables"] if entry["version"] == "2026-06"
    ]
    allowed = {"patch-06-to-07", "patch-06-to-07-bulk"}
    manifest["patches"] = [
        entry for entry in manifest["patches"] if entry.get("id") in allowed
    ]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (destination / "tables/table-2026-09.json").unlink(missing_ok=True)


def tamper_single_base(destination: Path) -> None:
    """A one-base archive whose only base is replaced without metadata change."""

    drop_newer_base(destination)
    corrupt_base_same_size_same_mtime(destination)


SCENARIOS = {
    "missing-patch": break_missing_patch,
    "no-chain": break_every_route,
    "bad-bases": corrupt_both_bases,
    "swap-base": corrupt_base_same_size_same_mtime,
    "single-base": drop_newer_base,
    "single-base-swap": tamper_single_base,
}


def usage(program: str) -> str:
    names = "|".join(SCENARIOS)
    return (
        f"usage: {program} {{{names}}} DESTINATION\n"
        f"       {program} --apply {{{names}}} EXISTING_ARCHIVE"
    )


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    apply_only = False
    if arguments and arguments[0] == "--apply":
        apply_only = True
        arguments = arguments[1:]
    if len(arguments) != 2 or arguments[0] not in SCENARIOS:
        print(usage(argv[0]), file=sys.stderr)
        return 2
    scenario, destination = arguments[0], Path(arguments[1]).resolve()
    if apply_only:
        if not (destination / "manifest.json").is_file():
            print(f"not an archive: {destination}", file=sys.stderr)
            return 2
    else:
        copy_sample(destination)
    SCENARIOS[scenario](destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
