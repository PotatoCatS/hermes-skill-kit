#!/usr/bin/env python3
"""JSON bridge between the dashboard and the SKM proposal tooling.

Run with the system Python (PyYAML is available there); the dashboard venv
does not need PyYAML. Read operations delegate validation to the existing
``proposal_lint`` module. The only writes are:

  * approve: fill approval fields in one pending proposal and move it to
    proposals/approved/ (the exact contract apply_proposal.py expects);
  * reject: mark one pending proposal rejected and move it to rejected/.

Skills are never touched here. Applying and rolling back a change still run
``apply_proposal.py`` / ``rollback_change.py`` from the dashboard server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    json.dump({"ok": False, "error": "PyYAML is required for the bridge"}, sys.stdout)
    sys.stdout.write("\n")
    raise SystemExit(1)

PROPOSAL_ID_RE = re.compile(r"^SKM-P-\d{8}-\d{3}$")
STATUSES = ("pending", "approved", "rejected")
NEGATIONS = ("不批准", "不予批准", "拒绝", "驳回")


def emit(obj: dict, code: int = 0) -> int:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code


def fail(message: str, code: int = 0) -> int:
    return emit({"ok": False, "error": message}, code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lint(base_dir: Path):
    scripts = base_dir / "scripts"
    if not scripts.is_dir():
        raise RuntimeError(f"scripts directory missing: {scripts}")
    sys.path.insert(0, str(scripts))
    import proposal_lint as pl  # noqa: WPS433 - deliberate local import

    return pl


def proposal_files(base_dir: Path, status: str):
    folder = base_dir / "proposals" / status
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.yaml")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith(".") or path.name.upper().startswith(("TEMPLATE", "README")):
            continue
        out.append(path)
    for path in sorted(folder.glob("*.yml")):
        if path.is_file() and not path.is_symlink() and not path.name.startswith("."):
            out.append(path)
    return out


def load_proposal(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("提案顶层不是映射（YAML mapping）")
    return data


def iter_proposals(base_dir: Path):
    for status in STATUSES:
        for path in proposal_files(base_dir, status):
            try:
                data = load_proposal(path)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                yield status, path, None, str(exc)
                continue
            yield status, path, data, None


def find_proposal(base_dir: Path, proposal_id: str, only_status: str | None = None):
    for status, path, data, error in iter_proposals(base_dir):
        if only_status and status != only_status:
            continue
        if data and data.get("proposal_id") == proposal_id:
            return status, path, data, error
    return None


def touched_skills(data: dict) -> list[str]:
    skills = []
    for rel in data.get("target_files") or []:
        parts = Path(str(rel).replace("\\", "/")).parts
        if len(parts) >= 2 and parts[0] == "skills":
            skills.append(parts[1])
    return list(dict.fromkeys(skills))


def hash_rows(base_dir: Path, hermes_home: Path, data: dict) -> list[dict]:
    baseline = data.get("baseline_sha256") or {}
    rows = []
    for rel, expected in baseline.items():
        target = hermes_home / str(rel)
        exists = target.is_file() and not target.is_symlink()
        current = sha256_file(target) if exists else None
        rows.append(
            {
                "path": str(rel),
                "baseline": str(expected),
                "current": current,
                "exists": exists,
                "match": bool(exists and current == expected),
                "bytes": target.stat().st_size if exists else None,
            }
        )
    return rows


def diff_info(lint, data: dict) -> dict:
    diff = data.get("diff") or ""
    text = diff if isinstance(diff, str) else ""
    paths: list[str] = []
    error = None
    try:
        paths = lint.diff_paths(text) if text.strip() else []
    except ValueError as exc:
        error = str(exc)
    return {
        "diff": text,
        "diff_sha256": lint.diff_sha256(text),
        "diff_paths": paths,
        "diff_lines": len(text.splitlines()),
        "diff_parse_error": error,
    }


def summary(lint, base_dir: Path, hermes_home: Path, status: str, path: Path, data: dict) -> dict:
    if data is None:
        return {
            "proposal_id": path.stem,
            "status": status,
            "file": path.name,
            "parse_error": "无法解析",
            "hash_ok": False,
        }
    rows = hash_rows(base_dir, hermes_home, data)
    info = diff_info(lint, data)
    quote = data.get("approval_quote")
    return {
        "proposal_id": data.get("proposal_id"),
        "status": status,
        "file": path.name,
        "target_skill": data.get("target_skill"),
        "skills": touched_skills(data),
        "priority": data.get("priority"),
        "created_at": str(data.get("created_at") or ""),
        "author": data.get("author"),
        "problem": " ".join(str(data.get("problem") or "").split())[:220],
        "files": len(data.get("target_files") or []),
        "hash_ok": bool(rows) and all(r["match"] for r in rows),
        "hash_rows": rows,
        "diff_paths": info["diff_paths"],
        "diff_parse_error": info["diff_parse_error"],
        "diff_sha256": info["diff_sha256"],
        "approval_quote": quote if isinstance(quote, str) else None,
        "approved_diff_sha256": data.get("approved_diff_sha256"),
        "change_id": data.get("change_id"),
        "result": data.get("result"),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def detail(lint, base_dir: Path, hermes_home: Path, status: str, path: Path, data: dict) -> dict:
    info = diff_info(lint, data)
    result = data.get("result")
    check_current = result != "applied"
    check_diff = result != "applied" and status != "rejected"
    try:
        errors = lint.validate_proposal(
            data,
            path,
            hermes_home,
            base_dir,
            check_current=check_current,
            check_unique=True,
            check_diff=check_diff,
        )
    except Exception as exc:  # validation must not hide the detail view
        errors = [f"校验器异常：{type(exc).__name__}: {exc}"]
    raw = path.read_text(encoding="utf-8", errors="replace")
    approved_hash = data.get("approved_diff_sha256")
    return {
        "ok": True,
        "summary": summary(lint, base_dir, hermes_home, status, path, data),
        "fields": {
            "proposal_id": data.get("proposal_id"),
            "target_skill": data.get("target_skill"),
            "skills": touched_skills(data),
            "multi_skill_justification": data.get("multi_skill_justification"),
            "priority": data.get("priority"),
            "created_at": str(data.get("created_at") or ""),
            "author": data.get("author"),
            "problem": data.get("problem"),
            "evidence": data.get("evidence") or [],
            "proposed_changes": data.get("proposed_changes"),
            "expected_benefit": data.get("expected_benefit"),
            "regression_risk": data.get("regression_risk"),
            "verification_plan": data.get("verification_plan") or [],
            "backup_plan": data.get("backup_plan"),
            "rollback_plan": data.get("rollback_plan"),
            "approval_status": data.get("approval_status"),
            "approved_by": data.get("approved_by"),
            "approved_at": data.get("approved_at"),
            "approval_quote": data.get("approval_quote"),
            "approved_diff_sha256": approved_hash,
            "change_id": data.get("change_id"),
            "result": data.get("result"),
            "rejection_reason": data.get("rejection_reason"),
        },
        "hash_rows": hash_rows(base_dir, hermes_home, data),
        "diff": info["diff"],
        "diff_sha256": info["diff_sha256"],
        "diff_paths": info["diff_paths"],
        "diff_parse_error": info["diff_parse_error"],
        "lint_errors": errors,
        "lint_ok": not errors,
        "post_apply": result == "applied",
        "approved_hash_matches": (
            bool(approved_hash) and str(approved_hash) == info["diff_sha256"]
        ),
        "raw": raw[:200_000],
    }


def quote_problem(quote: str, proposal_id: str, skills: list[str]) -> str | None:
    quote = str(quote or "").strip()
    if len(quote) < 6:
        return "批准语过短"
    if proposal_id not in quote:
        return "批准语必须包含提案编号"
    if "批准" not in quote:
        return "批准语必须包含“批准”二字"
    for negation in NEGATIONS:
        if negation in quote:
            return f"批准语包含否定词“{negation}”"
    if len(skills) > 1:
        missing = [s for s in skills if s not in quote]
        if missing:
            return "多技能提案的批准语必须点名每个技能：" + "、".join(missing)
    return None


def set_scalar(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}:\s*[^\n]*$")
    line = f"{key}: {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"


def atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skm-bridge-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def append_log(base_dir: Path, title: str, lines: list[str]) -> None:
    path = base_dir / "logs" / "change-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"\n## {title}\n\n")
        for line in lines:
            fh.write(f"- {line}\n")


def cmd_list(args) -> int:
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()
    lint = load_lint(base_dir)
    items = []
    errors = []
    counts = {status: 0 for status in STATUSES}
    for status, path, data, error in iter_proposals(base_dir):
        counts[status] += 1
        if data is None:
            errors.append(f"{status}/{path.name}: {error}")
            items.append(
                {
                    "proposal_id": path.stem,
                    "status": status,
                    "file": path.name,
                    "parse_error": error,
                    "hash_ok": False,
                }
            )
            continue
        items.append(summary(lint, base_dir, hermes_home, status, path, data))

    def sort_key(item):
        return (item.get("created_at") or "", item.get("proposal_id") or "")

    items.sort(key=sort_key, reverse=True)
    return emit({"ok": True, "counts": counts, "items": items, "errors": errors})


def cmd_detail(args) -> int:
    if not PROPOSAL_ID_RE.match(args.proposal_id):
        return fail("提案编号格式无效")
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()
    found = find_proposal(base_dir, args.proposal_id)
    if not found:
        return fail("提案不存在")
    status, path, data, error = found
    if data is None:
        return fail(f"提案无法解析：{error}")
    lint = load_lint(base_dir)
    return emit(detail(lint, base_dir, hermes_home, status, path, data))


def cmd_approve(args) -> int:
    if not PROPOSAL_ID_RE.match(args.proposal_id):
        return fail("提案编号格式无效")
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()
    found = find_proposal(base_dir, args.proposal_id)
    if not found:
        return fail("提案不存在")
    status, path, data, error = found
    if data is None:
        return fail(f"提案无法解析：{error}")
    if status != "pending":
        return fail(f"只有 pending 提案可以批准（当前在 {status}/）")
    if data.get("approval_status") != "pending":
        return fail("approval_status 不是 pending")
    quote = str(args.quote or "").strip()
    skills = touched_skills(data)
    problem = quote_problem(quote, args.proposal_id, skills)
    if problem:
        return fail(f"批准语无效：{problem}")

    lint = load_lint(base_dir)
    # Pre-check everything except uniqueness (the same proposal still exists in
    # pending/, so a uniqueness scan would count it twice).
    errors = lint.validate_proposal(
        data,
        path,
        hermes_home,
        base_dir,
        check_current=True,
        check_unique=False,
        check_diff=True,
    )
    if errors:
        return emit({"ok": False, "error": "提案未通过校验，不能批准", "lint_errors": errors})

    for other_status, other_path, other_data, _ in iter_proposals(base_dir):
        if other_path.resolve() == path.resolve():
            continue
        if other_data and other_data.get("proposal_id") == args.proposal_id:
            return fail(f"提案编号重复：{other_status}/{other_path.name}")

    original_text = path.read_text(encoding="utf-8")
    original_mode = path.stat().st_mode & 0o7777
    text = original_text
    approved_hash = lint.diff_sha256(str(data.get("diff") or ""))
    text = set_scalar(text, "approval_status", "approved")
    text = set_scalar(text, "approved_by", json.dumps("user", ensure_ascii=False))
    text = set_scalar(text, "approved_at", json.dumps(now_iso(), ensure_ascii=False))
    text = set_scalar(text, "approval_quote", json.dumps(quote, ensure_ascii=False))
    text = set_scalar(text, "approved_diff_sha256", json.dumps(approved_hash, ensure_ascii=False))

    approved_dir = base_dir / "proposals" / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    target = approved_dir / path.name
    if target.exists() or target.is_symlink():
        return fail(f"目标文件已存在：{target}")

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skm-approve-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, target)
        path.unlink()
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass

    # Full check in the approved location; restore the pending file on failure.
    try:
        approved_data = load_proposal(target)
        errors = lint.validate_proposal(
            approved_data,
            target,
            hermes_home,
            base_dir,
            check_current=True,
            check_unique=True,
            check_diff=True,
        )
    except Exception as exc:
        errors = [f"校验器异常：{type(exc).__name__}: {exc}"]
    if errors:
        atomic_write(path, original_text, original_mode)
        try:
            target.unlink()
        except OSError:
            pass
        return emit({"ok": False, "error": "批准后校验失败，已还原", "lint_errors": errors})

    append_log(
        base_dir,
        f"{datetime.now():%Y-%m-%d}（Web 批准 {args.proposal_id}）",
        [
            f"proposal: {args.proposal_id}；target: {data.get('target_skill')}",
            f"approval_quote: 「{' '.join(quote.split())[:160]}」",
            f"approved_diff_sha256: {approved_hash}",
            f"file: proposals/approved/{path.name}",
        ],
    )
    lint2 = lint
    return emit(
        {
            "ok": True,
            "message": f"已批准 {args.proposal_id}",
            "proposal": detail(
                lint2, base_dir, hermes_home, "approved", target, approved_data
            ),
        }
    )


def cmd_reject(args) -> int:
    if not PROPOSAL_ID_RE.match(args.proposal_id):
        return fail("提案编号格式无效")
    reason = " ".join(str(args.reason or "").split())
    if len(reason) < 3:
        return fail("请填写拒绝原因（至少 3 个字符）")
    if len(reason) > 500:
        return fail("拒绝原因过长")
    base_dir = args.base_dir.resolve()
    hermes_home = args.hermes_home.resolve()
    found = find_proposal(base_dir, args.proposal_id)
    if not found:
        return fail("提案不存在")
    status, path, data, error = found
    if data is None:
        return fail(f"提案无法解析：{error}")
    if status != "pending":
        return fail(f"只有 pending 提案可以拒绝（当前在 {status}/）")

    text = path.read_text(encoding="utf-8")
    text = set_scalar(text, "approval_status", "rejected")
    text = set_scalar(text, "rejected_by", json.dumps("user", ensure_ascii=False))
    text = set_scalar(text, "rejected_at", json.dumps(now_iso(), ensure_ascii=False))
    text = set_scalar(text, "rejection_reason", json.dumps(reason, ensure_ascii=False))

    rejected_dir = base_dir / "proposals" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    target = rejected_dir / path.name
    if target.exists() or target.is_symlink():
        return fail(f"目标文件已存在：{target}")

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skm-reject-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, target)
        path.unlink()
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass

    append_log(
        base_dir,
        f"{datetime.now():%Y-%m-%d}（Web 拒绝 {args.proposal_id}）",
        [
            f"proposal: {args.proposal_id}；target: {data.get('target_skill')}",
            f"rejection_reason: {reason}",
            f"file: proposals/rejected/{path.name}",
        ],
    )
    return emit(
        {
            "ok": True,
            "message": f"已拒绝 {args.proposal_id}",
            "proposal_id": args.proposal_id,
            "status": "rejected",
        }
    )


def cmd_next_change_id(args: argparse.Namespace) -> int:
    """Allocate the next change id with apply_proposal's own allocator."""
    scripts = args.base_dir / "scripts"
    if not scripts.is_dir():
        return fail(f"scripts directory missing: {scripts}")
    sys.path.insert(0, str(scripts))
    import apply_proposal as ap  # noqa: WPS433 - deliberate local import

    today = datetime.now().strftime("%Y%m%d")
    return emit({"ok": True, "change_id": ap.allocate_change_id(args.base_dir, today)})


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="SKM proposal bridge for the dashboard")
    default_hermes = Path(
        os.environ.get("SKD_HERMES_HOME")
        or os.environ.get("HERMES_HOME")
        or (Path.home() / ".hermes")
    )
    default_base = Path(
        os.environ.get("SKD_BASE_DIR") or (default_hermes / "skill-management")
    )
    ap.add_argument("--base-dir", type=Path, default=default_base)
    ap.add_argument("--hermes-home", type=Path, default=default_hermes)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("detail")
    p.add_argument("--proposal-id", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--quote", required=True)
    p = sub.add_parser("reject")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--reason", required=True)
    sub.add_parser("next-change-id")
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "detail":
            return cmd_detail(args)
        if args.command == "approve":
            return cmd_approve(args)
        if args.command == "reject":
            return cmd_reject(args)
        if args.command == "next-change-id":
            return cmd_next_change_id(args)
    except Exception as exc:  # keep stdout JSON-only
        return emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 1)
    return fail("未知命令")


if __name__ == "__main__":
    raise SystemExit(main())
