import json
import os
import shutil
from pathlib import Path

import pytest

from app.archive import Archive, document_digest, parse_json_bytes

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_archive"


def copy_archive(tmp_path: Path) -> Path:
    target = tmp_path / "archive"
    shutil.copytree(SAMPLE, target)
    return target


def write_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def replace_preserving_metadata(path: Path, document: dict) -> None:
    """Swap a table for valid JSON with the same size and restored timestamps.

    This models the reported attack: a mounted file is replaced by content
    that is still parseable, has the same byte count and leaves mtime/size
    untouched, so only re-reading and re-hashing the file can detect it.
    """

    stat = path.stat()
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    assert len(payload) == stat.st_size, "replacement must keep the byte count"
    path.write_bytes(payload)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (stat.st_mtime_ns, stat.st_size)


def bump_one_price(table: dict) -> None:
    """Change one price in place without changing its digit-width."""

    for key in sorted(table):
        value = table[key]["price_fen"]
        if value >= 100:
            table[key]["price_fen"] = value - 1 if value % 10 else value + 1
            return
    raise AssertionError("no price to bump")


def test_resolves_every_release_from_the_sample():
    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    assert [r.version for r in manifest.releases] == [
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
        "2026-10",
    ]
    for release in manifest.releases:
        resolution = archive.resolve(release.version)
        assert resolution.ok, resolution.reason
        assert resolution.chain[0].kind == "base"
        assert resolution.chain[-1].to_version == release.version
        assert document_digest(resolution.document) == manifest.digests[release.version]


def test_chooses_cheapest_chain_and_reports_packages():
    archive = Archive(SAMPLE)
    resolution = archive.resolve("2026-10")
    assert resolution.ok
    used = [step.package for step in resolution.chain]
    # The smallest chain starts at the September base table, not at June.
    assert used == ["tables/table-2026-09.json", "patches/patch-09-to-10.json"]
    assert resolution.package_bytes == 44739 + 15224


def test_skips_bulkier_equivalent_package():
    archive = Archive(SAMPLE)
    resolution = archive.resolve("2026-07")
    assert resolution.ok
    assert [step.package_id for step in resolution.chain] == [
        "base:2026-06",
        "patch-06-to-07",
    ]


def test_honours_digest_when_table_is_tampered(tmp_path):
    root = copy_archive(tmp_path)
    # Corrupt the September base table in a way that still parses as JSON.
    path = root / "tables/table-2026-09.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    first_key = min(table)
    table[first_key]["price_fen"] += 1
    path.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert resolution.ok, resolution.reason
    # It fell back to the June base table and patched forward.
    assert resolution.chain[0].package == "tables/table-2026-06.json"
    assert [issue.package for issue in resolution.issues] == ["tables/table-2026-09.json"]
    assert resolution.issues[0].kind == "摘要不符"
    assert resolution.attempts[0].ok is False
    assert resolution.attempts[-1].ok is True


def test_missing_package_switches_chain(tmp_path):
    root = copy_archive(tmp_path)
    (root / "patches/patch-09-to-10.json").unlink()
    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert resolution.ok, resolution.reason
    assert [(i.package, i.kind) for i in resolution.issues] == [
        ("patches/patch-09-to-10.json", "缺包")
    ]
    assert all(step.package != "patches/patch-09-to-10.json" for step in resolution.chain)
    assert resolution.chain[0].package == "tables/table-2026-06.json"


def test_broken_patch_json_switches_chain(tmp_path):
    root = copy_archive(tmp_path)
    patch_path = root / "patches/patch-09-to-10.json"
    patch_path.write_text("{ not json", encoding="utf-8")
    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert resolution.ok, resolution.reason
    assert any(issue.kind == "坏包" for issue in resolution.issues)
    assert resolution.issues[0].package == "patches/patch-09-to-10.json"


def test_patch_that_applies_but_yields_wrong_digest_switches_chain(tmp_path):
    root = copy_archive(tmp_path)
    patch_path = root / "patches/patch-09-to-10.json"
    operations = json.loads(patch_path.read_text(encoding="utf-8"))
    # Replace an existing value with a different but type-correct value.
    for operation in operations:
        if operation["op"] == "replace" and "price_fen" in operation["value"]:
            operation["value"]["price_fen"] += 1
            break
    else:  # pragma: no cover - sample always has a replace
        raise AssertionError("sample patch has no replace operation")
    patch_path.write_text(json.dumps(operations, ensure_ascii=False), encoding="utf-8")
    manifest = load_manifest(root)
    manifest["patches"] = [
        entry
        for entry in manifest["patches"]
        if entry["id"] not in {"patch-06-to-10", "patch-07-to-10"}
    ]
    write_manifest(root, manifest)

    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert not resolution.ok
    kinds = {issue.package: issue.kind for issue in resolution.issues}
    assert kinds["patches/patch-09-to-10.json"] == "摘要不符"
    assert "均不可用" in resolution.reason


def test_declared_byte_count_mismatch_is_detected(tmp_path):
    root = copy_archive(tmp_path)
    manifest = load_manifest(root)
    for entry in manifest["patches"]:
        if entry["id"] == "patch-09-to-10":
            entry["bytes"] += 1
    write_manifest(root, manifest)

    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert resolution.ok
    assert any(issue.kind == "字节数不符" for issue in resolution.issues)
    assert resolution.chain[0].package == "tables/table-2026-06.json"


def test_no_usable_chain_when_every_route_is_broken(tmp_path):
    root = copy_archive(tmp_path)
    for patch in (root / "patches").glob("*.json"):
        patch.write_text("[]", encoding="utf-8")
    # Remove the September base table and break every package.
    (root / "tables/table-2026-09.json").unlink()

    archive = Archive(root)
    resolution = archive.resolve("2026-10")
    assert not resolution.ok
    assert resolution.chain == []
    assert resolution.attempts
    assert all(not attempt.ok for attempt in resolution.attempts)
    assert "均不可用" in resolution.reason
    assert "缺包" in resolution.reason
    assert "tables/table-2026-09.json" in resolution.reason


def test_reports_missing_manifest(tmp_path):
    archive = Archive(tmp_path)
    resolution = archive.resolve("2026-10")
    assert not resolution.ok
    assert "清单" in resolution.reason


def test_reports_unreachable_target(tmp_path):
    root = copy_archive(tmp_path)
    manifest = load_manifest(root)
    manifest["releases"].append({"version": "2026-12", "label": "2026 年 12 月月报"})
    manifest["digests"]["2026-12"] = "0" * 64
    write_manifest(root, manifest)
    archive = Archive(root)
    resolution = archive.resolve("2026-12")
    assert not resolution.ok
    assert "无法通过更正包到达" in resolution.reason


def test_same_size_same_mtime_base_swap_is_detected_on_next_resolve(tmp_path):
    root = copy_archive(tmp_path)
    base_path = root / "tables/table-2026-06.json"
    archive = Archive(root)

    first = archive.resolve("2026-06")
    assert first.ok, first.reason
    first_digest = document_digest(first.document)

    table = json.loads(base_path.read_text(encoding="utf-8"))
    bump_one_price(table)
    replace_preserving_metadata(base_path, table)

    second = archive.resolve("2026-06")
    assert not second.ok
    assert second.document is None
    assert [(issue.package, issue.kind) for issue in second.issues] == [
        ("tables/table-2026-06.json", "摘要不符")
    ]
    assert second.chain == []
    assert (root / "tables/table-2026-06.json").is_file()
    # Sanity: the on-disk content really is different from what was verified.
    assert document_digest(json.loads(base_path.read_text(encoding="utf-8"))) != first_digest


def test_swapped_base_falls_back_to_another_verified_chain(tmp_path):
    root = copy_archive(tmp_path)
    base_path = root / "tables/table-2026-09.json"
    archive = Archive(root)

    assert archive.resolve("2026-09").ok

    table = json.loads(base_path.read_text(encoding="utf-8"))
    bump_one_price(table)
    replace_preserving_metadata(base_path, table)

    second = archive.resolve("2026-09")
    assert second.ok, second.reason
    assert [step.package for step in second.chain] == [
        "tables/table-2026-06.json",
        "patches/patch-06-to-09.json",
    ]
    assert [(issue.package, issue.kind) for issue in second.issues] == [
        ("tables/table-2026-09.json", "摘要不符")
    ]
    assert document_digest(second.document) == second.chain[-1].sha256


def test_swapped_base_with_no_alternative_chain_is_rejected(tmp_path):
    root = copy_archive(tmp_path)
    manifest = load_manifest(root)
    # Leave only the June base table reachable to 2026-07 by the cheap patch.
    manifest["base_tables"] = [
        entry
        for entry in manifest["base_tables"]
        if entry["version"] == "2026-06"
    ]
    manifest["patches"] = [
        entry
        for entry in manifest["patches"]
        if entry["id"] in {"patch-06-to-07", "patch-06-to-07-bulk"}
    ]
    write_manifest(root, manifest)

    archive = Archive(root)
    assert archive.resolve("2026-07").ok

    base_path = root / "tables/table-2026-06.json"
    table = json.loads(base_path.read_text(encoding="utf-8"))
    bump_one_price(table)
    replace_preserving_metadata(base_path, table)

    second = archive.resolve("2026-07")
    assert not second.ok
    assert second.chain == []
    assert "均不可用" in second.reason
    kinds = {issue.package: issue.kind for issue in second.issues}
    assert kinds["tables/table-2026-06.json"] == "摘要不符"


def test_patch_swap_preserving_metadata_is_detected_on_next_resolve(tmp_path):
    root = copy_archive(tmp_path)
    patch_path = root / "patches/patch-06-to-07.json"
    archive = Archive(root)
    assert archive.resolve("2026-07").ok

    operations = json.loads(patch_path.read_text(encoding="utf-8"))
    for operation in operations:
        if operation["op"] == "replace" and "price_fen" in operation["value"]:
            operation["value"]["price_fen"] += 1
            break
    else:  # pragma: no cover - sample always has a replace
        raise AssertionError("sample patch has no replace operation")
    stat = patch_path.stat()
    payload = json.dumps(operations, ensure_ascii=False).encode("utf-8")
    if len(payload) <= stat.st_size:
        payload += b" " * (stat.st_size - len(payload))
    else:
        pytest.skip("cannot keep the patch byte count with this sample")
    patch_path.write_bytes(payload)
    os.utime(patch_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    second = archive.resolve("2026-07")
    # The tampered package is rejected; the bulkier equivalent package from
    # the same version is used instead.
    assert second.ok, second.reason
    assert [step.package_id for step in second.chain] == [
        "base:2026-06",
        "patch-06-to-07-bulk",
    ]
    kinds = {(issue.package, issue.kind) for issue in second.issues}
    assert ("patches/patch-06-to-07.json", "摘要不符") in kinds


def test_strict_json_parsing():
    with pytest.raises(ValueError):
        parse_json_bytes(b'{"a": 1, "a": 2}')
    with pytest.raises(ValueError):
        parse_json_bytes(b'{"a": NaN}')
    with pytest.raises(ValueError):
        parse_json_bytes(b"\xff\xfe")
