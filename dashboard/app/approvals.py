"""Approval-center service layer.

Thin, allowlisted wrapper around the existing Phase-5 pipeline:

  proposals  -> app/skm_bridge.py (PyYAML, system Python; writes only one YAML)
  apply      -> scripts/apply_proposal.py (dry-run enforced first)
  rollback   -> scripts/rollback_change.py
  backups    -> read-only scans of backups/<change_id>/

No shell interpolation, no user-supplied paths or flags. The only arguments
taken from HTTP are proposal ids / change ids that must match the existing
SKM-* patterns.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import audit_log
from .safety import BASE, HERMES_HOME

DASH_DIR = Path(__file__).resolve().parent.parent
BRIDGE = DASH_DIR / "app" / "skm_bridge.py"
PYTHON = os.environ.get("SKD_PYTHON_BIN", "/usr/bin/python3")
PROPOSAL_ID_RE = re.compile(r"^SKM-P-\d{8}-\d{3}$")
CHANGE_ID_RE = re.compile(r"^SKM-C-\d{8}-\d{3}$")
MAX_OUTPUT = 200_000
APPLY_TIMEOUT = 180
ROLLBACK_TIMEOUT = 120
BRIDGE_TIMEOUT = 60

_EXEC_LOCK = threading.Lock()


class ApprovalError(Exception):
    status_code = 400


class ApprovalNotFound(ApprovalError):
    status_code = 404


class ApprovalConflict(ApprovalError):
    status_code = 409


def _env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": os.environ.get("HOME") or str(Path.home()),
    }


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n…（输出已截断，共 {len(text)} 字符）"


def _run(cmd: list[str], timeout: int) -> dict:
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
        raise ApprovalError(f"命令超时（{timeout}s），已终止") from None
    return {"code": proc.returncode, "stdout": _clip(stdout), "stderr": _clip(stderr)}


def _bridge(command: str, extra: list[str] | None = None) -> dict:
    cmd = [
        PYTHON,
        str(BRIDGE),
        "--base-dir",
        str(BASE),
        "--hermes-home",
        str(HERMES_HOME),
        command,
    ] + list(extra or [])
    result = _run(cmd, BRIDGE_TIMEOUT)
    raw = (result["stdout"] or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        detail = (result["stderr"] or raw or "bridge produced no JSON").strip()
        raise ApprovalError(f"提案桥接失败：{detail[:800]}") from None
    if not isinstance(data, dict):
        raise ApprovalError("提案桥接返回了非对象 JSON")
    return data


def _require_id(value: str, pattern: re.Pattern, label: str) -> str:
    value = str(value or "").strip()
    if not pattern.match(value):
        raise ApprovalError(f"{label}格式无效")
    return value


def list_proposals() -> dict:
    data = _bridge("list")
    if not data.get("ok"):
        raise ApprovalError(str(data.get("error") or "无法读取提案"))
    return data


def proposal_detail(proposal_id: str) -> dict:
    pid = _require_id(proposal_id, PROPOSAL_ID_RE, "提案编号")
    data = _bridge("detail", ["--proposal-id", pid])
    if not data.get("ok"):
        message = str(data.get("error") or "提案不存在")
        if "不存在" in message:
            raise ApprovalNotFound(message)
        raise ApprovalError(message)
    return data


def approve(proposal_id: str, quote: str) -> dict:
    pid = _require_id(proposal_id, PROPOSAL_ID_RE, "提案编号")
    quote = str(quote or "")
    data = _bridge("approve", ["--proposal-id", pid, "--quote", quote])
    if not data.get("ok"):
        message = str(data.get("error") or "批准失败")
        audit_log.log_action("approve", f"{pid} 失败：{message}", ok=False)
        error = ApprovalError(message)
        error.detail = data
        raise error
    audit_log.log_action("approve", f"{pid} 已批准")
    return data


def reject(proposal_id: str, reason: str) -> dict:
    pid = _require_id(proposal_id, PROPOSAL_ID_RE, "提案编号")
    data = _bridge("reject", ["--proposal-id", pid, "--reason", str(reason or "")])
    if not data.get("ok"):
        message = str(data.get("error") or "拒绝失败")
        audit_log.log_action("reject", f"{pid} 失败：{message}", ok=False)
        raise ApprovalError(message)
    audit_log.log_action("reject", f"{pid} 已拒绝")
    return data


def _find_proposal_path(proposal_id: str, status: str) -> Path:
    pid = _require_id(proposal_id, PROPOSAL_ID_RE, "提案编号")
    folder = (BASE / "proposals" / status).resolve()
    if not folder.is_dir():
        raise ApprovalNotFound(f"proposals/{status}/ 不存在")
    for path in sorted(folder.glob("*.yaml")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r"(?m)^proposal_id:\s*[\"']?([A-Za-z0-9-]+)", text)
        if match and match.group(1) == pid:
            resolved = path.resolve()
            if folder not in resolved.parents:
                continue
            return resolved
    raise ApprovalNotFound(f"proposals/{status}/ 中没有 {pid}")


def _change_id_of(proposal_file: Path) -> str | None:
    try:
        text = proposal_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?m)^change_id:\s*[\"']?(SKM-C-\d{8}-\d{3})", text)
    return match.group(1) if match else None


def _hash_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _rolled_back_in_log(change_id: str) -> bool:
    """rollback_change.py records success only in logs/change-log.md."""
    path = BASE / "logs" / "change-log.md"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > 2_000_000:
                fh.seek(size - 2_000_000)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return re.search(re.escape(change_id) + r"\s*回滚", text) is not None


def _change_phase_from_manifest(change_id: str) -> str:
    """Derive the real phase from target hashes.

    rollback_change.py marks the proposal as rolled_back but leaves the
    backup's STATE.json at "applied", so STATE alone cannot be trusted for
    re-apply decisions. Hash comparison against MANIFEST.json can.
    """
    folder = BASE / "backups" / change_id
    manifest = _read_json(folder / "MANIFEST.json")
    state = _read_json(folder / "STATE.json")
    declared = str(state.get("phase") or "")
    if declared in ("applying", "backing_up"):
        return declared
    if _rolled_back_in_log(change_id):
        return "rolled_back"
    entries = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    root = HERMES_HOME.resolve()
    before = after = total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            return "unknown"
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            return "unknown"
        total += 1
        if not target.is_file() or target.is_symlink():
            return "missing"
        sha = _hash_file(target)
        if sha == entry.get("sha256_before"):
            before += 1
        elif sha == entry.get("sha256_after"):
            after += 1
        else:
            return "mixed"
    if not total:
        return declared or "unknown"
    if before == total:
        return "rolled_back"
    if after == total:
        return "applied"
    return "mixed"


def _next_change_id() -> str:
    """Ask the bridge for the next id using apply_proposal's own allocator."""
    data = _bridge("next-change-id")
    cid = str(data.get("change_id") or "")
    if not data.get("ok") or not CHANGE_ID_RE.match(cid):
        raise ApprovalError(str(data.get("error") or "无法分配新的变更编号"))
    return cid


def _prior_change(proposal_file: Path) -> tuple[str | None, str]:
    """Return (change id recorded on the proposal, backup phase)."""
    existing = _change_id_of(proposal_file)
    if not existing:
        return None, ""
    backup = BASE / "backups" / existing
    if not backup.is_dir() or backup.is_symlink():
        return existing, "missing"
    return existing, _change_phase_from_manifest(existing)


def apply_proposal(proposal_id: str, dry_run: bool, confirmation: str) -> dict:
    pid = _require_id(proposal_id, PROPOSAL_ID_RE, "提案编号")
    path = _find_proposal_path(pid, "approved")
    if not dry_run and str(confirmation or "").strip() != f"执行 {pid}":
        raise ApprovalError(f"正式执行的确认语必须为：执行 {pid}")

    script = BASE / "scripts" / "apply_proposal.py"
    if not script.is_file():
        raise ApprovalError(f"执行脚本不存在：{script}")

    # A proposal keeps its change_id after rollback. Re-applying the same
    # proposal must NOT reuse that backup directory, so allocate a new change
    # id through the pipeline's own allocator. Applied or unknown states are
    # refused instead of guessed at.
    existing, phase = _prior_change(path)
    previous = None
    effective = None
    if existing:
        if phase == "applied":
            raise ApprovalConflict(
                f"该提案已执行（变更 {existing}）且尚未回滚；"
                "请先在“变更与备份”中回滚，再重新执行"
            )
        if phase == "rolled_back":
            previous = existing
            effective = _next_change_id()
        elif phase == "missing":
            effective = _next_change_id()
        else:
            raise ApprovalConflict(
                f"变更 {existing} 当前状态为 {phase or '未知'}；"
                f"请先用 CLI 恢复：apply_proposal.py --recover {existing}"
            )
    notice = (
        f"该提案上次变更 {previous} 已回滚；本次将使用新的变更编号 {effective}，旧备份保留。"
        if previous
        else None
    )

    base_args = [
        PYTHON,
        str(script),
        str(path),
        "--hermes-home",
        str(HERMES_HOME),
        "--base-dir",
        str(BASE),
    ]
    if effective:
        base_args += ["--change-id", effective]

    if not _EXEC_LOCK.acquire(blocking=False):
        raise ApprovalConflict("另一个执行或回滚操作正在进行，请稍后再试")
    try:
        preflight = None
        if not dry_run:
            preflight = _run(base_args + ["--dry-run"], APPLY_TIMEOUT)
            if preflight["code"] != 0:
                audit_log.log_action("apply", f"{pid} dry-run 预检失败", ok=False)
                return {
                    "ok": False,
                    "stage": "dry-run",
                    "dry_run": True,
                    "code": preflight["code"],
                    "stdout": preflight["stdout"],
                    "stderr": preflight["stderr"],
                    "message": "预检未通过，未写入任何文件",
                    "previous_change_id": previous,
                    "reapply": bool(previous),
                    "notice": notice,
                }

        result = _run(base_args + (["--dry-run"] if dry_run else []), APPLY_TIMEOUT)
        change_id = effective if dry_run else _change_id_of(path)
        ok = result["code"] == 0
        audit_log.log_action(
            "apply",
            f"{pid} {'dry-run' if dry_run else 'apply'} code={result['code']}"
            + (f" change={change_id}" if change_id else "")
            + (f" reapply-after-rollback (was {previous})" if previous else ""),
            ok=ok,
        )
        return {
            "ok": ok,
            "stage": "dry-run" if dry_run else "apply",
            "dry_run": dry_run,
            "code": result["code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "change_id": change_id,
            "previous_change_id": previous,
            "reapply": bool(previous),
            "notice": notice,
            "preflight": preflight,
            "message": "预检通过，未写入任何文件"
            if (dry_run and ok)
            else ("执行完成" if ok else "执行失败，请查看输出与错误日志"),
        }
    finally:
        _EXEC_LOCK.release()


def rollback_change(change_id: str, confirmation: str) -> dict:
    cid = _require_id(change_id, CHANGE_ID_RE, "变更编号")
    if str(confirmation or "").strip() != f"回滚 {cid}":
        raise ApprovalError(f"回滚确认语必须为：回滚 {cid}")
    script = BASE / "scripts" / "rollback_change.py"
    if not script.is_file():
        raise ApprovalError(f"回滚脚本不存在：{script}")
    backup = BASE / "backups" / cid
    if not backup.is_dir() or backup.is_symlink():
        raise ApprovalNotFound(f"备份不存在：{cid}")

    if not _EXEC_LOCK.acquire(blocking=False):
        raise ApprovalConflict("另一个执行或回滚操作正在进行，请稍后再试")
    try:
        result = _run(
            [
                PYTHON,
                str(script),
                cid,
                "--hermes-home",
                str(HERMES_HOME),
                "--base-dir",
                str(BASE),
            ],
            ROLLBACK_TIMEOUT,
        )
        ok = result["code"] == 0
        audit_log.log_action("rollback", f"{cid} code={result['code']}", ok=ok)
        return {
            "ok": ok,
            "code": result["code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "change_id": cid,
            "message": "回滚完成" if ok else "回滚失败，请查看输出与错误日志",
        }
    finally:
        _EXEC_LOCK.release()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def changes() -> list[dict]:
    root = BASE / "backups"
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.name.startswith("."):
            continue
        manifest = _read_json(folder / "MANIFEST.json")
        state = _read_json(folder / "STATE.json")
        files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
        protected = (
            manifest.get("protected") if isinstance(manifest.get("protected"), list) else []
        )
        phase = (
            _change_phase_from_manifest(folder.name)
            if manifest and CHANGE_ID_RE.match(folder.name)
            else str(state.get("phase") or ("manual" if manifest else "unknown"))
        )
        out.append(
            {
                "change_id": str(manifest.get("change_id") or state.get("change_id") or folder.name),
                "dir_name": folder.name,
                "proposal_id": manifest.get("proposal_id") or state.get("proposal_id"),
                "phase": phase,
                "created_utc": manifest.get("created_utc")
                or state.get("created_utc")
                or _iso_from_ts(folder.stat().st_mtime),
                "files": len(files),
                "protected": len(protected),
                "has_manifest": bool(manifest),
                "has_state": bool(state),
                "has_rollback": (folder / "ROLLBACK.md").is_file(),
                "legacy": not CHANGE_ID_RE.match(folder.name) or not bool(manifest),
            }
        )
    out.sort(key=lambda x: str(x.get("created_utc") or ""), reverse=True)
    return out


def change_detail(change_id: str) -> dict:
    cid = str(change_id or "").strip()
    if not cid or cid in (".", "..") or "/" in cid or "\\" in cid:
        raise ApprovalError("变更编号格式无效")
    root = (BASE / "backups").resolve()
    folder = (root / cid).resolve()
    if folder == root or root not in folder.parents:
        raise ApprovalError("变更编号越界")
    if not folder.is_dir() or folder.is_symlink():
        raise ApprovalNotFound(f"备份不存在：{cid}")
    manifest = _read_json(folder / "MANIFEST.json")
    state = _read_json(folder / "STATE.json")
    rollback_md = ""
    rb_path = folder / "ROLLBACK.md"
    if rb_path.is_file() and not rb_path.is_symlink():
        rollback_md = rb_path.read_text(encoding="utf-8", errors="replace")[:64_000]
    files = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        files.append(
            {
                "path": entry.get("path"),
                "bytes": entry.get("bytes"),
                "sha256_before": entry.get("sha256_before"),
                "sha256_after": entry.get("sha256_after"),
                "backup_rel": entry.get("backup_rel"),
            }
        )
    return {
        "change_id": cid,
        "dir_name": folder.name,
        "proposal_id": manifest.get("proposal_id") or state.get("proposal_id"),
        "phase": _change_phase_from_manifest(folder.name)
        if (manifest and CHANGE_ID_RE.match(folder.name))
        else str(state.get("phase") or ("manual" if manifest else "unknown")),
        "created_utc": manifest.get("created_utc") or state.get("created_utc"),
        "files": files,
        "protected": manifest.get("protected") or [],
        "state": state,
        "manifest_summary": {
            "operation": manifest.get("operation"),
            "hermes_home": manifest.get("hermes_home"),
            "created_utc": manifest.get("created_utc"),
            "migrated_utc": manifest.get("migrated_utc"),
        },
        "rollback_md": rollback_md,
        "has_rollback": bool(rollback_md),
    }


LOG_KINDS = {
    "change": BASE / "logs" / "change-log.md",
    "error": BASE / "logs" / "error-log.md",
    "actions": BASE / "logs" / "dashboard-actions.log",
}
LOG_MAX_BYTES = 300_000


def logs(kind: str = "change") -> dict:
    path = LOG_KINDS.get(kind)
    if path is None:
        raise ApprovalError("未知日志类型")
    if not path.is_file():
        return {"kind": kind, "exists": False, "content": "", "bytes": 0, "mtime": None}
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > LOG_MAX_BYTES:
            fh.seek(size - LOG_MAX_BYTES)
        data = fh.read()
    return {
        "kind": kind,
        "exists": True,
        "content": data.decode("utf-8", errors="replace"),
        "bytes": size,
        "truncated": size > LOG_MAX_BYTES,
        "mtime": _iso_from_ts(path.stat().st_mtime),
    }
