#!/usr/bin/env python3
"""Verify a change applied from an SKM proposal.

Checks the post-apply state: every target file changed, structural rules hold,
backup copies are intact, and protected files (registry, global config, cron
jobs) were not touched.

Usage:
    python3 verify_changes.py proposals/approved/SKM-P-20261103-001.yaml \
        --change-id SKM-C-20261103-001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import proposal_lint as pl

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("FATAL: PyYAML is required (python3 -m pip install pyyaml)\n")
    raise SystemExit(2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Verify an applied SKM change")
    ap.add_argument("proposal", type=Path)
    ap.add_argument("--change-id", default=None)
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


def verify(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    proposal_path = args.proposal.resolve()
    try:
        proposal = pl.load_proposal(proposal_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [f"cannot read proposal: {exc}"]

    # Structural validation; hashes are expected to differ after the patch.
    errors.extend(
        pl.validate_proposal(
            proposal,
            proposal_path,
            args.hermes_home,
            args.base_dir,
            check_current=False,
            check_unique=False,
            check_diff=False,
        )
    )

    baseline = proposal.get("baseline_sha256") or {}
    targets = proposal.get("target_files") or []
    for rel in targets:
        try:
            path = pl.safe_relpath(rel, args.hermes_home)
            pl.assert_no_symlink(path, args.hermes_home)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        if not path.is_file():
            errors.append(f"{rel}: target file is missing after apply")
            continue
        current = pl.sha256_file(path)
        if current == baseline.get(rel):
            errors.append(f"{rel}: file is unchanged from baseline")
        if path.name == "SKILL.md":
            errors.extend(pl.check_skill_structure(path, args.hermes_home))

    change_id = args.change_id or proposal.get("change_id")
    if change_id:
        backup_dir = args.base_dir / "backups" / str(change_id)
        manifest_path = backup_dir / "MANIFEST.json"
        if not manifest_path.is_file():
            errors.append(f"backup manifest missing: {manifest_path}")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                manifest = {}
                errors.append(f"cannot read backup manifest: {exc}")
            for entry in manifest.get("files", []):
                rel = entry.get("path", "")
                try:
                    # L-6: backup_rel must stay inside the backup directory.
                    backup_copy = pl.safe_backup_rel(entry.get("backup_rel"), backup_dir)
                    pl.assert_no_symlink(backup_copy, backup_dir)
                except (TypeError, ValueError) as exc:
                    errors.append(f"{rel}: invalid backup_rel in manifest: {exc}")
                    continue
                if backup_copy.is_symlink():
                    errors.append(f"{rel}: backup copy is a symlink")
                elif not backup_copy.is_file():
                    errors.append(f"{rel}: backup copy missing")
                elif pl.sha256_file(backup_copy) != entry.get("sha256_before"):
                    errors.append(f"{rel}: backup copy hash mismatch")
                try:
                    current_target = pl.safe_relpath(rel, args.hermes_home)
                    pl.assert_no_symlink(current_target, args.hermes_home)
                except ValueError as exc:
                    errors.append(f"{rel}: {exc}")
                    continue
                if current_target.is_file() and "sha256_after" in entry:
                    if pl.sha256_file(current_target) != entry["sha256_after"]:
                        errors.append(f"{rel}: current hash does not match recorded sha256_after")
            # M-1: protected entries carry their own root. The registry lives
            # under base_dir; config.yaml and cron/jobs.json under hermes_home.
            for item in manifest.get("protected") or []:
                rel = item.get("path", "")
                root = args.base_dir if item.get("root") == "base_dir" else args.hermes_home
                path = root / rel
                if not path.is_file():
                    errors.append(f"protected file missing: {item.get('root', '?')}:{rel}")
                elif pl.sha256_file(path) != item.get("sha256"):
                    errors.append(f"protected file was modified: {item.get('root', '?')}:{rel}")
            # L-1: the skills tree must still match the post-apply snapshot
            # (nothing moved after apply), and the before→after delta must be
            # exactly target_files (nothing else moved during apply). Legacy
            # manifests without snapshots skip this check.
            tree_after = manifest.get("tree_after")
            if isinstance(tree_after, dict) and tree_after:
                current_tree = pl.snapshot_tree(args.hermes_home)
                if current_tree != tree_after:
                    added = sorted(set(current_tree) - set(tree_after))
                    removed = sorted(set(tree_after) - set(current_tree))
                    changed = sorted(
                        p for p in set(current_tree) & set(tree_after)
                        if current_tree[p] != tree_after[p]
                    )
                    errors.append(
                        "skills tree differs from post-apply snapshot "
                        f"(added={len(added)}, removed={len(removed)}, changed={len(changed)})"
                    )
                    for path in (changed + added + removed)[:10]:
                        errors.append(f"  out-of-band: {path}")
                tree_before = manifest.get("tree_before")
                if isinstance(tree_before, dict):
                    touched = {
                        p for p in set(tree_before) | set(tree_after)
                        if tree_before.get(p) != tree_after.get(p)
                    }
                    targets = set(map(str, proposal.get("target_files") or []))
                    if touched != targets:
                        extra = sorted(touched - targets)[:5]
                        missing = sorted(targets - touched)[:5]
                        errors.append(
                            "applied change set differs from target_files "
                            f"(extra={extra}, missing={missing})"
                        )
    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    errors = verify(args)
    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("PASS: structural checks, changed-file set, backup and protected files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
