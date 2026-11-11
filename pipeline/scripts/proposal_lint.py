#!/usr/bin/env python3
"""Validate a skill-maintenance proposal (SKM-P-...).

Read-only. Exits 0 when the proposal is complete, current and applicable;
exits 1 with a list of errors otherwise.

Usage:
    python3 proposal_lint.py proposals/pending/SKM-P-20261103-001.yaml
    python3 proposal_lint.py <proposal> --hermes-home ~/.hermes \
        --base-dir ~/.hermes/skill-management
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("FATAL: PyYAML is required (python3 -m pip install pyyaml)\n")
    raise SystemExit(2)

PROPOSAL_ID_RE = re.compile(r"^SKM-P-\d{8}-\d{3}$")
CHANGE_ID_RE = re.compile(r"^SKM-C-\d{8}-\d{3}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_KEYS = [
    "proposal_id",
    "created_at",
    "priority",
    "target_skill",
    "target_files",
    "baseline_sha256",
    "problem",
    "evidence",
    "proposed_changes",
    "diff",
    "expected_benefit",
    "regression_risk",
    "verification_plan",
    "backup_plan",
    "rollback_plan",
    "approval_status",
]
STATUSES = {"pending", "approved", "rejected"}
PRIORITIES = {"P0", "P1", "P2"}
REFERENCE_RE = re.compile(r"`((?:references|templates)/[^`]+)`")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_diff(diff: str) -> str:
    """Normalize line endings and guarantee one trailing newline before hashing."""
    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def diff_sha256(diff: str) -> str:
    return hashlib.sha256(canonical_diff(diff).encode("utf-8")).hexdigest()


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("top level is not a mapping")
    return data


def load_proposal(path: Path) -> dict:
    return load_yaml(path)


def safe_relpath(rel: object, hermes_home: Path) -> Path:
    """Resolve a proposal-relative path without following symlinks.

    The returned path is lexical: it can still contain symlink components, so
    callers that read or write must follow up with assert_no_symlink().
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path is empty")
    rel = rel.strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(f"absolute path not allowed: {rel}")
    parts = Path(rel).parts
    if ".." in parts:
        raise ValueError(f"'..' not allowed: {rel}")
    if not rel.startswith("skills/"):
        raise ValueError(f"only paths under skills/ are allowed: {rel}")
    home = hermes_home.resolve()
    candidate = Path(os.path.normpath(str(home / rel)))
    if os.path.commonpath([str(candidate), str(home)]) != str(home):
        raise ValueError(f"path escapes hermes home: {rel}")
    return candidate


def assert_no_symlink(path: Path, hermes_home: Path) -> None:
    """Reject a path if the file or any component below home is a symlink."""
    home = hermes_home.resolve()
    try:
        rel_parts = path.relative_to(home).parts
    except ValueError as exc:
        raise ValueError(f"path escapes hermes home: {path}") from exc
    current = home
    for part in rel_parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink not allowed in path: {current.relative_to(home)}")


def safe_backup_rel(rel: object, backup_dir: Path) -> Path:
    """Resolve a manifest backup_rel without leaving backup_dir (lexical).

    Used by rollback_change and verify_changes (L-6). Callers that read or
    write the result must follow up with assert_no_symlink().
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("backup_rel is empty")
    rel = rel.strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ValueError(f"absolute path not allowed in backup_rel: {rel}")
    if ".." in Path(rel).parts:
        raise ValueError(f"'..' not allowed in backup_rel: {rel}")
    base = backup_dir.resolve()
    candidate = Path(os.path.normpath(str(base / rel)))
    if os.path.commonpath([str(candidate), str(base)]) != str(base):
        raise ValueError(f"backup_rel escapes backup dir: {rel}")
    return candidate


# Runtime files written by the gateway while skills are used; excluded from
# apply-time tree snapshots (L-1) so their writes never abort a real apply.
EXCLUDED_SNAPSHOT_NAMES = {".usage.json"}


def snapshot_tree(hermes_home: Path) -> dict[str, str]:
    """SHA-256 of every regular file under skills/, keyed by relative path."""
    tree: dict[str, str] = {}
    skills = hermes_home / "skills"
    if not skills.is_dir():
        return tree
    for dirpath, dirnames, filenames in os.walk(skills):
        dirnames.sort()
        for name in sorted(filenames):
            if name in EXCLUDED_SNAPSHOT_NAMES:
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            tree[path.relative_to(hermes_home).as_posix()] = sha256_file(path)
    return tree


def diff_paths(diff: str) -> list[str]:
    """Return the new-side paths declared by a unified diff."""
    if "GIT binary patch" in diff or "Binary files " in diff:
        raise ValueError("binary diffs are not supported")
    if "@@" not in diff:
        raise ValueError("diff has no hunk")
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].split("\t", 1)[0].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith("b/"):
                raw = raw[2:]
            paths.append(raw)
        elif line.startswith("rename from ") or line.startswith("rename to "):
            raise ValueError("rename diffs are not supported; use delete + add")
    if not paths:
        raise ValueError("diff declares no target paths")
    return paths


def git_apply_check(diff: str, hermes_home: Path) -> tuple[bool, str]:
    """Run `git apply --check` without touching the tree."""
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8", newline="\n") as fh:
        fh.write(diff if diff.endswith("\n") else diff + "\n")
        tmp = fh.name
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", tmp],
            cwd=str(hermes_home),
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def skill_names(hermes_home: Path) -> set[str]:
    """Names of every skill on disk, including .archive (for related_skills)."""
    names: set[str] = set()
    skills_root = hermes_home / "skills"
    if not skills_root.is_dir():
        return names
    for skill_md in skills_root.rglob("SKILL.md"):
        names.add(skill_md.parent.name)
        try:
            with open(skill_md, encoding="utf-8") as fh:
                text = fh.read()
            m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            if m:
                fm = yaml.safe_load(m.group(1))
                if isinstance(fm, dict) and isinstance(fm.get("name"), str):
                    names.add(fm["name"].strip())
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
    return names


def read_frontmatter(skill_md: Path) -> tuple[dict | None, str]:
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("missing YAML frontmatter block")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data, text


def check_skill_structure(skill_md: Path, hermes_home: Path) -> list[str]:
    errors: list[str] = []
    try:
        fm, text = read_frontmatter(skill_md)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return [f"{skill_md}: {exc}"]

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{skill_md}: frontmatter name is missing")
    elif name.strip() != skill_md.parent.name:
        errors.append(
            f"{skill_md}: name '{name}' does not match directory '{skill_md.parent.name}'"
        )
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append(f"{skill_md}: description is missing")
    elif len(desc) > 60:
        errors.append(f"{skill_md}: description is {len(desc)} chars (limit 60)")
    if "version" not in fm:
        errors.append(f"{skill_md}: version is missing")

    if b"\x00" in text.encode("utf-8"):
        errors.append(f"{skill_md}: contains NUL bytes")
    if "\t" in text:
        errors.append(f"{skill_md}: contains a tab character")

    known = skill_names(hermes_home)
    meta = fm.get("metadata") or {}
    if isinstance(meta, dict):
        hermes_meta = meta.get("hermes") or {}
        if isinstance(hermes_meta, dict):
            related = hermes_meta.get("related_skills") or []
            if isinstance(related, list):
                for rname in related:
                    if isinstance(rname, str) and rname.strip() not in known:
                        errors.append(
                            f"{skill_md}: related_skills names unknown skill '{rname}'"
                        )

    skill_dir = skill_md.parent
    for raw in REFERENCE_RE.findall(text):
        ref = raw.strip()
        if not ref or any(ch in ref for ch in "<>*{}…") or ref.endswith("/"):
            continue
        if not (skill_dir / ref).is_file():
            errors.append(f"{skill_md}: referenced file does not exist: {ref}")
    return errors


def validate_proposal(
    proposal: dict,
    proposal_path: Path,
    hermes_home: Path,
    base_dir: Path,
    *,
    check_current: bool = True,
    check_unique: bool = True,
    check_diff: bool = True,
) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in proposal:
            errors.append(f"missing required field: {key}")
    if errors:
        return errors

    pid = proposal.get("proposal_id")
    if not isinstance(pid, str) or not PROPOSAL_ID_RE.match(pid):
        errors.append(f"proposal_id must match SKM-P-YYYYMMDD-NNN, got {pid!r}")
    if proposal.get("approval_status") not in STATUSES:
        errors.append(f"approval_status must be one of {sorted(STATUSES)}")
    if proposal.get("priority") not in PRIORITIES:
        errors.append(f"priority must be one of {sorted(PRIORITIES)}")
    if not isinstance(proposal.get("target_skill"), str) or not proposal["target_skill"].strip():
        errors.append("target_skill is empty")

    files = proposal.get("target_files")
    if not isinstance(files, list) or not files:
        errors.append("target_files must be a non-empty list")
        files = []
    if len(set(map(str, files))) != len(files):
        errors.append("target_files contains duplicates")

    baseline = proposal.get("baseline_sha256")
    if not isinstance(baseline, dict) or not baseline:
        errors.append("baseline_sha256 must be a non-empty mapping")
        baseline = {}
    if files and set(map(str, baseline)) != set(map(str, files)):
        errors.append("baseline_sha256 keys must equal target_files")

    # M-4: derive the set of skills touched by target_files. Default is a
    # single skill; multi-skill proposals need an explicit justification and,
    # once approved, an approval_quote that names every touched skill.
    skills_touched: list[str] = []
    for rel in map(str, files):
        parts = Path(rel.replace("\\", "/")).parts
        if len(parts) >= 2 and parts[0] == "skills":
            skills_touched.append(parts[1])
    skills_touched = list(dict.fromkeys(skills_touched))
    if not skills_touched:
        errors.append("cannot derive any skill from target_files")
    elif proposal.get("target_skill") not in skills_touched:
        errors.append(
            f"target_skill {proposal.get('target_skill')!r} is not among the "
            f"skills touched by target_files: {skills_touched}"
        )
    if len(skills_touched) > 1:
        justification = proposal.get("multi_skill_justification")
        if not isinstance(justification, str) or not justification.strip():
            errors.append(
                "proposal touches multiple skills "
                f"({', '.join(skills_touched)}); multi_skill_justification is required"
            )
        elif proposal.get("approval_status") == "approved":
            quote = str(proposal.get("approval_quote") or "")
            missing = [s for s in skills_touched if s not in quote]
            if missing:
                errors.append(
                    "multi-skill approval must name every touched skill in "
                    f"approval_quote; missing: {', '.join(missing)}"
                )

    if not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
        errors.append("evidence must be a non-empty list")

    diff = proposal.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        errors.append("diff is empty")
        diff = ""
    elif check_diff:
        try:
            touched = diff_paths(diff)
            if files and set(touched) != set(map(str, files)):
                errors.append(
                    "diff target paths differ from target_files: "
                    f"diff={sorted(set(touched))} target_files={sorted(set(map(str, files)))}"
                )
            for rel in touched:
                safe_relpath(rel, hermes_home)
        except ValueError as exc:
            errors.append(f"diff validation failed: {exc}")
        ok, msg = git_apply_check(diff, hermes_home)
        if not ok:
            errors.append(f"git apply --check failed: {msg}")

    if proposal.get("approval_status") == "approved":
        approved_hash = proposal.get("approved_diff_sha256")
        if not isinstance(approved_hash, str) or not HEX64_RE.match(approved_hash):
            errors.append(
                "approved proposal requires approved_diff_sha256 "
                "(64 lowercase hex characters)"
            )
        elif diff and diff_sha256(diff) != approved_hash:
            errors.append(
                "diff does not match approved_diff_sha256; the proposal must be re-approved"
            )
        # L-3: the approval sentence must be a positive approval, not a
        # substring coincidence ("我不批准 SKM-P-…" must never pass).
        quote = str(proposal.get("approval_quote") or "")
        if isinstance(pid, str) and pid not in quote:
            errors.append("approved proposal approval_quote must contain the proposal_id")
        if "批准" not in quote:
            errors.append("approval_quote must contain 批准")
        for negation in ("不批准", "不予批准", "拒绝", "驳回"):
            if negation in quote:
                errors.append(f"approval_quote contains a negation: {negation}")
                break

    for rel, expected in (baseline or {}).items():
        try:
            target = safe_relpath(rel, hermes_home)
            assert_no_symlink(target, hermes_home)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        if not isinstance(expected, str) or not HEX64_RE.match(expected):
            errors.append(f"{rel}: baseline hash is not 64 lowercase hex characters")
            continue
        if not target.is_file():
            errors.append(f"{rel}: target file does not exist")
            continue
        if target.is_symlink():
            errors.append(f"{rel}: target is a symlink")
            continue
        if check_current:
            actual = sha256_file(target)
            if actual != expected:
                errors.append(
                    f"{rel}: baseline hash mismatch (expected {expected[:16]}…, actual {actual[:16]}…)"
                )

    if check_unique and isinstance(pid, str):
        # L-8: recursive scan — duplicates may hide in nested subdirectories.
        for other in (base_dir / "proposals").rglob("*.yaml"):
            if other.resolve() == proposal_path.resolve():
                continue
            try:
                if load_yaml(other).get("proposal_id") == pid:
                    errors.append(f"proposal_id {pid} already used by {other}")
            except (OSError, ValueError, yaml.YAMLError):
                continue
    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Validate an SKM proposal")
    ap.add_argument("proposal", type=Path)
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
    ap.add_argument("--no-current-check", action="store_true", help="skip baseline hash comparison")
    ap.add_argument("--no-diff-check", action="store_true", help="skip git apply --check")
    ap.add_argument(
        "--print-diff-sha256",
        action="store_true",
        help="print the approved_diff_sha256 value for this proposal and exit",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    path = args.proposal.resolve()
    try:
        proposal = load_proposal(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"FAIL: cannot read proposal: {exc}")
        return 1

    if args.print_diff_sha256:
        print(diff_sha256(str(proposal.get("diff") or "")))
        return 0

    errors = validate_proposal(
        proposal,
        path,
        args.hermes_home,
        args.base_dir,
        check_current=not args.no_current_check,
        check_diff=not args.no_diff_check,
    )
    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"PASS: {path.name} ({proposal['proposal_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
