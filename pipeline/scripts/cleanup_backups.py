#!/usr/bin/env python3
"""M-3: retention policy for skill-management backups/.

Candidates: only SKM-C-YYYYMMDD-NNN directories containing a parseable
MANIFEST.json. Always kept: the --recent (default 3) most recent candidates
plus every backup referenced from logs/change-log.md or a proposal file.
Never touched: non-SKM-C directories (manual backups), directories without a
parseable MANIFEST.json (reported, never deleted), symlinks. If the keep-set
alone exceeds --max-backups the run refuses and deletes nothing. --dry-run
prints the plan and writes nothing at all (not even the log).

Usage:
    python3 cleanup_backups.py --dry-run
    python3 cleanup_backups.py --max-backups 10 --recent 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

CHANGE_ID_RE = re.compile(r"^SKM-C-\d{8}-\d{3}$")
CHANGE_ID_TEXT_RE = re.compile(r"SKM-C-\d{8}-\d{3}")
MAX_RECENT_DEFAULT = 3
MAX_BACKUPS_DEFAULT = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Retention for skill-management backups/")
    ap.add_argument("--base-dir", type=Path, default=Path(
        os.environ.get("SKM_BASE_DIR")
        or (Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "skill-management")
    ))
    ap.add_argument("--max-backups", type=int, default=MAX_BACKUPS_DEFAULT,
                    help="hard cap on SKM-C backup directories (default 10)")
    ap.add_argument("--recent", type=int, default=MAX_RECENT_DEFAULT,
                    help="always keep this many most recent candidates (default 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan only; delete nothing and write no log")
    return ap.parse_args(sys.argv[1:] if argv is None else argv)


def referenced_change_ids(base_dir: Path) -> set[str]:
    """change_ids mentioned in logs/change-log.md or any proposal YAML."""
    found: set[str] = set()
    log = base_dir / "logs" / "change-log.md"
    if log.is_file():
        try:
            found.update(CHANGE_ID_TEXT_RE.findall(log.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            pass
    proposals = base_dir / "proposals"
    if proposals.is_dir():
        for path in proposals.glob("*/*.yaml"):
            try:
                found.update(CHANGE_ID_TEXT_RE.findall(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return found


def scan_backups(base_dir: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (candidates, manual, no_manifest).

    candidates are SKM-C dirs with a parseable MANIFEST.json, sorted
    oldest-first (fixed-width names sort chronologically). manual are all
    other directories; no_manifest are SKM-C dirs that could not be
    verified and are therefore never deleted.
    """
    backups = base_dir / "backups"
    candidates: list[Path] = []
    manual: list[Path] = []
    no_manifest: list[Path] = []
    if not backups.is_dir():
        return candidates, manual, no_manifest
    for child in sorted(backups.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if not CHANGE_ID_RE.match(child.name):
            manual.append(child)
            continue
        manifest = child / "MANIFEST.json"
        try:
            json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            no_manifest.append(child)
            continue
        candidates.append(child)
    return candidates, manual, no_manifest


def append_log(base_dir: Path, deleted: list[str], failures: list[str]) -> None:
    path = base_dir / "logs" / "backup-cleanup-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {stamp}\n\n")
        for name in deleted:
            fh.write(f"- deleted: {name}\n")
        for msg in failures:
            fh.write(f"- deletion failure: {msg}\n")
        if not deleted and not failures:
            fh.write("- nothing to delete\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_dir = args.base_dir.resolve()
    if args.max_backups < 1:
        print("FAIL: --max-backups must be >= 1")
        return 1
    if args.recent < 1:
        print("FAIL: --recent must be >= 1")
        return 1

    candidates, manual, no_manifest = scan_backups(base_dir)
    refs = referenced_change_ids(base_dir)

    keep: dict[str, Path] = {d.name: d for d in candidates if d.name in refs}
    for d in candidates[-args.recent:]:  # candidates are oldest-first
        keep[d.name] = d

    print(f"candidates (SKM-C with MANIFEST): {len(candidates)}")
    print(f"referenced: {sorted(refs)}")
    print(f"manual (never touched): {[d.name for d in manual]}")
    print(f"skipped (no parseable MANIFEST, never deleted): {[d.name for d in no_manifest]}")
    print(f"kept: {sorted(keep)}")

    if len(keep) > args.max_backups:
        print(
            f"FAIL: keep-set has {len(keep)} backups which exceeds "
            f"--max-backups={args.max_backups}; refusing to delete anything"
        )
        return 1

    to_delete: list[Path] = []
    remaining = len(candidates)
    for d in candidates:  # oldest first
        if remaining <= args.max_backups:
            break
        if d.name in keep:
            continue
        to_delete.append(d)
        remaining -= 1

    print(f"to delete: {[d.name for d in to_delete]}")

    if not to_delete:
        print("PASS: nothing to delete")
        if not args.dry_run:
            append_log(base_dir, [], [])
        return 0

    if args.dry_run:
        print(f"DRY-RUN: {len(to_delete)} backup(s) would be deleted; nothing written")
        return 0

    deleted: list[str] = []
    failures: list[str] = []
    for d in to_delete:
        try:
            shutil.rmtree(d)
        except OSError as exc:
            failures.append(f"{d.name}: {exc}")
            continue
        if d.exists():
            failures.append(f"{d.name}: still exists after rmtree")
            continue
        deleted.append(d.name)

    append_log(base_dir, deleted, failures)

    if failures:
        print(f"FAIL: {len(failures)} deletion failure(s); see logs/backup-cleanup-log.md")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print(f"PASS: deleted {len(deleted)} backup(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
