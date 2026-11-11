#!/usr/bin/env python3
"""Roll back an applied SKM change from its backup.

Restores every file recorded in backups/<change_id>/MANIFEST.json, verifies the
pre-change hashes, and marks the originating proposal as rolled_back. Running
it twice is safe: the second run reports that the tree is already restored.

Usage:
    python3 rollback_change.py SKM-C-20261103-001
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import proposal_lint as pl

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("FATAL: PyYAML is required (python3 -m pip install pyyaml)\n")
    raise SystemExit(2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Roll back an applied SKM change")
    ap.add_argument("change_id")
    ap.add_argument("--hermes-home", type=Path,
                    default=Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")))
    ap.add_argument(
        "--base-dir",
        type=Path,
        default=Path(
            os.environ.get("SKM_BASE_DIR")
            or (Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "skill-management")
        ),
    )
    return ap.parse_args(argv)


def find_proposal(base_dir: Path, proposal_id: str) -> Path | None:
    # L-8: recursive scan — proposal files may live at any depth under
    # proposals/ (pending/, approved/, rejected/, or user-created subdirs).
    for path in (base_dir / "proposals").rglob("*.yaml"):
        try:
            if pl.load_yaml(path).get("proposal_id") == proposal_id:
                return path
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return None


def mark_proposal(path: Path, result: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?m)^result:.*$")
    if pattern.search(text):
        text = pattern.sub(f"result: {result}", text, count=1)
    else:
        text = text.rstrip("\n") + f"\nresult: {result}\n"
    # Atomic write: temp file + os.replace, preserving the original mode.
    mode = path.stat().st_mode if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skm-proposal-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_log(path: Path, title: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {title}\n\n")
        for line in lines:
            fh.write(f"- {line}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()

    # L-5: change_id must be a single plain directory name; anything with a
    # path separator or parent reference is rejected before any path is built.
    cid = args.change_id
    if (
        not isinstance(cid, str)
        or not cid.strip()
        or cid in (".", "..")
        or "/" in cid
        or "\\" in cid
    ):
        print(f"FAIL: invalid change_id {cid!r} (must be a plain directory name)")
        return 1

    backup_root = base_dir / "backups"
    backup_dir = backup_root / cid
    if (
        os.path.commonpath([str(os.path.normpath(str(backup_dir))), str(backup_root)])
        != str(backup_root)
        or backup_dir.is_symlink()
        or backup_root.is_symlink()
    ):
        print(f"FAIL: change_id escapes the backups directory: {cid!r}")
        return 1
    manifest_path = backup_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        print(f"FAIL: backup manifest not found: {manifest_path}")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read manifest: {exc}")
        return 1

    # L-2: when the manifest names a proposal, its record must still exist;
    # otherwise the rollback would proceed with no audit trail ("unknown").
    # Legacy manifests without a proposal_id (e.g. migrated manual backups)
    # are allowed through.
    proposal: str | None = None
    proposal_path: Path | None = None
    declared = manifest.get("proposal_id")
    if isinstance(declared, str) and declared.strip():
        proposal_path = find_proposal(base_dir, declared)
        if proposal_path is None:
            print(f"FAIL: proposal record not found: {declared}; refusing to roll back")
            return 1
        proposal = declared

    entries = manifest.get("files") or []
    if not entries:
        print("FAIL: manifest lists no files")
        return 1

    plan: list[tuple[dict, Path, Path]] = []
    errors: list[str] = []
    for entry in entries:
        rel = entry.get("path")
        try:
            target = pl.safe_relpath(rel, hermes_home)
            pl.assert_no_symlink(target, hermes_home)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        backup_copy: Path
        try:
            # L-6: backup_rel must stay inside the backup directory.
            backup_copy = pl.safe_backup_rel(entry.get("backup_rel"), backup_dir)
            pl.assert_no_symlink(backup_copy, backup_dir)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        if not backup_copy.is_file():
            errors.append(f"{rel}: backup copy missing")
            continue
        actual = pl.sha256_file(backup_copy)
        if actual != entry.get("sha256_before"):
            errors.append(f"{rel}: backup copy hash mismatch")
            continue
        if target.exists() and target.is_symlink():
            errors.append(f"{rel}: target became a symlink; refusing to write")
            continue
        # L-7: a target edited after apply (hash matches neither the recorded
        # before nor after state) is someone else's work — refuse to overwrite.
        sha_after = entry.get("sha256_after")
        if (
            target.is_file()
            and isinstance(sha_after, str)
            and sha_after
            and pl.sha256_file(target) not in (entry["sha256_before"], sha_after)
        ):
            errors.append(
                f"{rel}: target changed since apply (matches neither sha256_before "
                "nor sha256_after); refusing to overwrite"
            )
            continue
        plan.append((entry, target, backup_copy))

    if errors:
        print(f"FAIL: {len(errors)} error(s); nothing was restored")
        for err in errors:
            print(f"  - {err}")
        return 1

    # L-4: protected files are hash-checked but never restored by rollback.
    # If one changed since apply, warn loudly instead of silently reporting a
    # clean rollback while the protected file stays tampered.
    protected_warnings: list[str] = []
    for item in manifest.get("protected") or []:
        rel = item.get("path", "")
        root = base_dir if item.get("root") == "base_dir" else hermes_home
        path = root / rel
        if not path.is_file():
            protected_warnings.append(f"{item.get('root', '?')}:{rel} (missing)")
        elif pl.sha256_file(path) != item.get("sha256"):
            protected_warnings.append(f"{item.get('root', '?')}:{rel} (hash changed)")
    if protected_warnings:
        print("WARNING: protected file(s) changed since apply; rollback restores only target_files:")
        for warning in protected_warnings:
            print(f"  - {warning}")

    # Restore loop (M-2): per-file fault tolerance. A failure on one file must
    # not abort the remaining restores, must not be reported as full success,
    # and must leave the backup in place so a re-run can retry.
    changed = False
    restored: list[str] = []
    failures: list[str] = []
    for entry, target, backup_copy in plan:
        if target.is_file() and pl.sha256_file(target) == entry["sha256_before"]:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic restore: copy to a sibling temp file, then os.replace.
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".skm-rollback-")
            try:
                os.close(fd)
                shutil.copy2(backup_copy, tmp_name)
                os.replace(tmp_name, target)
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            if pl.sha256_file(target) != entry["sha256_before"]:
                raise OSError("restored hash mismatch")
            changed = True
            restored.append(entry["path"])
        except OSError as exc:
            failures.append(f"{entry['path']}: {exc}")

    if protected_warnings:
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d}（{args.change_id} 回滚受保护文件告警）",
            [
                f"change_id: {args.change_id}；proposal: {proposal or 'unknown'}",
                "protected files changed since apply; not restored by rollback: "
                + "; ".join(protected_warnings),
            ],
        )

    if failures:
        if proposal_path:
            mark_proposal(proposal_path, "rollback_failed")
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d}（{args.change_id} 回滚部分失败）",
            [
                f"change_id: {args.change_id}；proposal: {proposal or 'unknown'}",
                f"restored: {', '.join(restored) or '（无）'}",
                "failures: " + "; ".join(failures),
                "backup retained; re-run rollback to retry the failed files",
            ],
        )
        print(f"FAIL: {args.change_id} partially rolled back ({len(failures)} file(s) failed)")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    if proposal_path:
        mark_proposal(proposal_path, "rolled_back")

    if changed:
        append_log(
            base_dir / "logs" / "change-log.md",
            f"{datetime.now():%Y-%m-%d}（{args.change_id} 回滚）",
            [
                f"change_id: {args.change_id}；proposal: {proposal or 'unknown'}",
                f"restored: {', '.join(e['path'] for e, _, _ in plan)}",
                "hashes verified against MANIFEST.json",
            ],
        )
        print(f"PASS: rolled back {args.change_id} ({len(plan)} file(s) checked)")
    else:
        print(f"PASS: {args.change_id} is already at its pre-change state; nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
