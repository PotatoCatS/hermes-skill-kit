#!/usr/bin/env python3
"""Apply an approved SKM proposal: re-check hashes, back up, patch, verify.

Refuses to run unless the proposal lives in proposals/approved/, carries
approval_status=approved with a non-empty approval_quote, and every baseline
hash still matches. On verification failure the change is rolled back
automatically. A STATE.json journal is kept in the backup directory so a
crash mid-apply can be recovered with --recover.

Usage:
    python3 apply_proposal.py proposals/approved/SKM-P-20261103-001.yaml
    python3 apply_proposal.py <proposal> --dry-run
    python3 apply_proposal.py --recover SKM-C-20261103-001
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import proposal_lint as pl

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("FATAL: PyYAML is required (python3 -m pip install pyyaml)\n")
    raise SystemExit(2)

CHANGE_ID_RE = re.compile(r"^SKM-C-\d{8}-\d{3}$")
# M-1: protected paths with explicit roots. The real registry lives under
# base_dir (skill-management/registry/skills.json); config.yaml and
# cron/jobs.json live under hermes_home. Missing or symlinked protected files
# abort the apply — never silently skipped.
PROTECTED = [
    {"path": "registry/skills.json", "root": "base_dir"},
    {"path": "config.yaml", "root": "hermes_home"},
    {"path": "cron/jobs.json", "root": "hermes_home"},
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Apply an approved SKM proposal")
    ap.add_argument("proposal", type=Path, nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--change-id", default=None)
    ap.add_argument(
        "--recover",
        metavar="CHANGE_ID",
        default=None,
        help="recover from a crash: complete or clean up backups/<CHANGE_ID>",
    )
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


def allocate_change_id(base_dir: Path, today: str) -> str:
    used: set[int] = set()
    backups = base_dir / "backups"
    if backups.is_dir():
        for child in backups.iterdir():
            if child.is_dir() and CHANGE_ID_RE.match(child.name):
                day, seq = child.name[6:14], child.name[15:]
                if day == today:
                    used.add(int(seq))
    log = base_dir / "logs" / "change-log.md"
    if log.is_file():
        for m in re.finditer(r"SKM-C-" + today + r"-(\d{3})", log.read_text(encoding="utf-8")):
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"SKM-C-{today}-{n:03d}"


def write_manifest(
    backup_dir: Path,
    entries: list[dict],
    protected: list[dict],
    proposal: dict,
    tree_before: dict[str, str],
) -> None:
    manifest = {
        "operation": "apply SKM proposal",
        "proposal_id": proposal.get("proposal_id"),
        "change_id": entries[0].get("change_id") if entries else None,
        "created_utc": now_utc(),
        "hermes_home": str(Path(proposal.get("_hermes_home", ""))) if proposal.get("_hermes_home") else None,
        "files": entries,
        "protected": protected,
        # L-1: full skills-tree snapshots; verify_changes.py uses them to
        # prove the applied change set equals target_files and nothing else
        # moved after apply. .usage.json runtime files are excluded.
        "tree_before": tree_before,
    }
    (backup_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def append_log(path: Path, title: str, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {title}\n\n")
        for line in lines:
            fh.write(f"- {line}\n")


def set_proposal_fields(path: Path, change_id: str, result: str) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in (("change_id", f'"{change_id}"'), ("result", result)):
        pattern = re.compile(rf"(?m)^{key}:.*$")
        if pattern.search(text):
            text = pattern.sub(f"{key}: {value}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{key}: {value}\n"
    # M-5: atomic write (temp file + os.replace), so a crash cannot leave a
    # half-written proposal YAML.
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


def write_state(backup_dir: Path, state: dict) -> None:
    """M-5: atomically persist the apply journal (STATE.json)."""
    path = backup_dir / "STATE.json"
    fd, tmp = tempfile.mkstemp(dir=str(backup_dir), prefix=".STATE-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_state(backup_dir: Path, phase: str) -> None:
    path = backup_dir / "STATE.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    state["phase"] = phase
    state["updated_utc"] = now_utc()
    write_state(backup_dir, state)


def find_proposal_path(base_dir: Path, proposal_id: str) -> Path | None:
    if not proposal_id:
        return None
    for path in (base_dir / "proposals").glob("*/*.yaml"):
        try:
            if pl.load_yaml(path).get("proposal_id") == proposal_id:
                return path
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return None


def recover_change(change_id: str, base_dir: Path, hermes_home: Path) -> int:
    """M-5: recover from a crash detected via backups/<change_id>/STATE.json."""
    if not CHANGE_ID_RE.match(change_id):
        print(f"FAIL: invalid change_id {change_id!r}")
        return 1
    backup_dir = base_dir / "backups" / change_id
    state_path = backup_dir / "STATE.json"
    if not state_path.is_file():
        print(f"PASS: no STATE.json for {change_id}; nothing to recover")
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read STATE.json: {exc}")
        return 1
    phase = state.get("phase")
    if phase == "applied":
        print(f"PASS: {change_id} is already applied; nothing to recover")
        return 0
    targets = state.get("targets") or {}
    if phase == "backing_up":
        # Crash while backing up: nothing may have been applied yet. Only
        # clean up the partial backup if every target is still at baseline.
        changed_targets: list[str] = []
        for rel, before in targets.items():
            try:
                target = pl.safe_relpath(rel, hermes_home)
                pl.assert_no_symlink(target, hermes_home)
                if not target.is_file() or pl.sha256_file(target) != before:
                    changed_targets.append(rel)
            except (ValueError, OSError):
                changed_targets.append(rel)
        if changed_targets:
            print("FAIL: targets changed while backing up; manual inspection required")
            for rel in changed_targets:
                print(f"  - {rel}")
            return 1
        shutil.rmtree(backup_dir, ignore_errors=True)
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d}（{change_id} 恢复：清理半截备份）",
            ["partial backup removed; no target file was changed"],
        )
        print(f"PASS: removed partial backup {backup_dir}; no file was changed")
        return 0
    if phase == "applying":
        # Crash around git apply / verification. If everything is already at
        # its post-apply hash AND the proposal is marked applied, just finish
        # the journal. Otherwise restore from the backup.
        manifest_path = backup_dir / "MANIFEST.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FAIL: cannot read MANIFEST.json: {exc}")
            return 1
        files = manifest.get("files") or []
        if not files:
            print("FAIL: MANIFEST lists no files")
            return 1
        all_after = True
        for entry in files:
            try:
                target = pl.safe_relpath(entry["path"], hermes_home)
                pl.assert_no_symlink(target, hermes_home)
                if pl.sha256_file(target) != entry.get("sha256_after"):
                    all_after = False
                    break
            except (ValueError, OSError):
                all_after = False
                break
        proposal_path = find_proposal_path(base_dir, state.get("proposal_id", ""))
        result = None
        if proposal_path is not None:
            try:
                result = pl.load_yaml(proposal_path).get("result")
            except (OSError, ValueError, yaml.YAMLError):
                result = None
        if all_after and result == "applied":
            update_state(backup_dir, "applied")
            print(f"PASS: {change_id} is fully applied; registration completed")
            return 0
        rollback_script = Path(__file__).with_name("rollback_change.py")
        proc = subprocess.run(
            [
                sys.executable,
                str(rollback_script),
                change_id,
                "--base-dir",
                str(base_dir),
                "--hermes-home",
                str(hermes_home),
            ],
            capture_output=True,
            text=True,
        )
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d}（{change_id} 恢复：applying 中断）",
            [f"recover restored from backup; rollback exit {proc.returncode}"],
        )
        return proc.returncode
    print(f"FAIL: unknown phase {phase!r} in STATE.json")
    return 1


def git_apply(diff: str, hermes_home: Path, check_only: bool) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8", newline="\n") as fh:
        fh.write(diff if diff.endswith("\n") else diff + "\n")
        tmp = fh.name
    try:
        cmd = ["git", "apply"]
        if check_only:
            cmd.append("--check")
        cmd.append(tmp)
        proc = subprocess.run(cmd, cwd=str(hermes_home), capture_output=True, text=True)
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def rollback(change_id: str, base_dir: Path, hermes_home: Path) -> None:
    script = Path(__file__).with_name("rollback_change.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            change_id,
            "--base-dir",
            str(base_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        print(f"CRITICAL: rollback failed for {change_id}; backups are under {base_dir / 'backups' / change_id}")
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()

    if args.recover:
        return recover_change(args.recover, base_dir, hermes_home)
    if args.proposal is None:
        print("FAIL: a proposal path is required unless --recover is used")
        return 1
    proposal_path = args.proposal.resolve()

    if proposal_path.parent.name != "approved":
        print("FAIL: only proposals under proposals/approved/ may be applied")
        return 1
    approved_root = (base_dir / "proposals" / "approved").resolve()
    if approved_root not in proposal_path.parents:
        print(f"FAIL: proposal is not inside {approved_root}")
        return 1
    try:
        proposal = pl.load_proposal(proposal_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"FAIL: cannot read proposal: {exc}")
        return 1

    if proposal.get("approval_status") != "approved":
        print("FAIL: approval_status is not approved")
        return 1
    for key in ("approved_by", "approved_at", "approval_quote"):
        if not proposal.get(key):
            print(f"FAIL: approved proposal is missing {key}")
            return 1
    if str(proposal["proposal_id"]) not in str(proposal["approval_quote"]):
        print("FAIL: approval_quote does not name the proposal_id")
        return 1

    errors = pl.validate_proposal(
        proposal, proposal_path, hermes_home, base_dir, check_current=True, check_unique=True
    )
    if errors:
        print(f"FAIL: {len(errors)} validation error(s)")
        for err in errors:
            print(f"  - {err}")
        return 1

    today = datetime.now().strftime("%Y%m%d")
    change_id = args.change_id or proposal.get("change_id") or allocate_change_id(base_dir, today)
    if not CHANGE_ID_RE.match(str(change_id)):
        print(f"FAIL: invalid change_id {change_id!r}")
        return 1
    backup_dir = base_dir / "backups" / change_id
    if backup_dir.exists():
        print(f"FAIL: backup directory already exists: {backup_dir}")
        return 1

    diff = proposal["diff"]
    ok, msg = git_apply(diff, hermes_home, check_only=True)
    if not ok:
        print(f"FAIL: git apply --check failed: {msg}")
        return 1

    if args.dry_run:
        print(f"PASS (dry-run): proposal valid, patch applies, change_id would be {change_id}")
        print("No backup or file was written.")
        return 0

    # M-5: persist the journal before touching anything so a crash leaves a
    # recoverable trail. phase: backing_up -> applying -> applied.
    backup_dir.mkdir(parents=True)
    write_state(
        backup_dir,
        {
            "change_id": change_id,
            "proposal_id": proposal["proposal_id"],
            "phase": "backing_up",
            "created_utc": now_utc(),
            "targets": {rel: proposal["baseline_sha256"][rel] for rel in proposal["target_files"]},
        },
    )

    # Backup first; abort the whole operation if any copy fails.
    manifest_entries: list[dict] = []
    protected: list[dict] = []
    (backup_dir / "files").mkdir()
    try:
        for rel in proposal["target_files"]:
            src = pl.safe_relpath(rel, hermes_home)
            pl.assert_no_symlink(src, hermes_home)
            dst = backup_dir / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            before = pl.sha256_file(src)
            if before != proposal["baseline_sha256"][rel]:
                raise RuntimeError(f"{rel}: hash changed while backing up")
            manifest_entries.append(
                {
                    "change_id": change_id,
                    "path": rel,
                    "bytes": src.stat().st_size,
                    "sha256_before": before,
                    "backup_rel": str(dst.relative_to(backup_dir)),
                }
            )
        for spec in PROTECTED:
            root = base_dir if spec["root"] == "base_dir" else hermes_home
            path = root / spec["path"]
            if path.is_symlink():
                raise RuntimeError(f"protected file is a symlink: {spec['root']}:{spec['path']}")
            if not path.is_file():
                raise RuntimeError(f"protected file missing: {spec['root']}:{spec['path']}")
            protected.append(
                {"path": spec["path"], "root": spec["root"], "sha256": pl.sha256_file(path)}
            )
    except Exception as exc:  # backup must be complete before any write
        shutil.rmtree(backup_dir, ignore_errors=True)
        print(f"FAIL: backup aborted: {exc}")
        return 1

    # L-1: snapshot the whole skills tree before any write; combined with the
    # post-apply snapshot it proves the applied change set == target_files.
    tree_before = pl.snapshot_tree(hermes_home)

    write_manifest(backup_dir, manifest_entries, protected, {**proposal, "_hermes_home": hermes_home}, tree_before)
    (backup_dir / "ROLLBACK.md").write_text(
        "# Rollback\n\n"
        f"Change: `{change_id}`  Proposal: `{proposal['proposal_id']}`\n\n"
        "```bash\n"
        f"python3 {base_dir}/scripts/rollback_change.py {change_id}\n"
        "```\n\n"
        "Restores every target file to its pre-change bytes and re-checks the hashes.\n",
        encoding="utf-8",
    )
    update_state(backup_dir, "applying")

    ok, msg = git_apply(diff, hermes_home, check_only=False)
    if not ok:
        print(f"FAIL: git apply failed, rolling back: {msg}")
        rollback(change_id, base_dir, hermes_home)
        update_state(backup_dir, "rolled_back")
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d} {change_id}",
            [f"apply failed: {msg}", "rolled back automatically"],
        )
        set_proposal_fields(proposal_path, change_id, "failed")
        return 1

    # Record post-apply hashes + tree snapshot, then run structural verification.
    manifest_path = backup_dir / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        target = pl.safe_relpath(entry["path"], hermes_home)
        pl.assert_no_symlink(target, hermes_home)
        entry["sha256_after"] = pl.sha256_file(target)
    manifest["tree_after"] = pl.snapshot_tree(hermes_home)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verify_script = Path(__file__).with_name("verify_changes.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(verify_script),
            str(proposal_path),
            "--change-id",
            change_id,
            "--base-dir",
            str(base_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print("FAIL: verification failed, rolling back")
        if proc.stderr.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        rollback(change_id, base_dir, hermes_home)
        update_state(backup_dir, "rolled_back")
        append_log(
            base_dir / "logs" / "error-log.md",
            f"{datetime.now():%Y-%m-%d} {change_id}",
            [f"verification failed for {proposal['proposal_id']}", "rolled back automatically"],
        )
        set_proposal_fields(proposal_path, change_id, "failed")
        return 1

    set_proposal_fields(proposal_path, change_id, "applied")
    update_state(backup_dir, "applied")
    quote = " ".join(str(proposal["approval_quote"]).split())[:120]
    append_log(
        base_dir / "logs" / "change-log.md",
        f"{datetime.now():%Y-%m-%d}（{change_id}）",
        [
            f"change_id: {change_id}；proposal: {proposal['proposal_id']}；skill: {proposal['target_skill']}",
            f"files: {', '.join(proposal['target_files'])}",
            f"approval_quote: 「{quote}」",
            f"approved_diff_sha256: {proposal.get('approved_diff_sha256')}",
            f"backup: {backup_dir}",
        ],
    )
    print(f"PASS: applied {change_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
