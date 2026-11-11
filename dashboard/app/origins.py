"""Infer how a skill came to exist: official / hermes / user / unknown.

Evidence is taken in this order, first hit wins:

1. A manual decision stored in this dashboard's own ``data/skill-origins.json``.
2. The bundled manifest, or a ``builtin`` / ``builtin-shadow`` source type.
3. The skill-hub lock file (installed from the official catalog).
4. The curator ledger ``create`` event:
   - actor ``curator``                     -> hermes (self-generated)
   - actor ``agent`` in a ``cron_`` session -> hermes (autonomous run)
   - actor ``agent`` in a user session     -> user (the user asked for it)
5. Nothing matched -> unknown ("待确认" in the UI).

Nothing here writes outside ``data/`` next to this package. The Hermes skill tree
and its registry are only read.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .safety import SKILLS_DIR

DASHBOARD_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SKD_DATA_DIR", str(DASHBOARD_DIR / "data")))
OVERLAY_PATH = DATA_DIR / "skill-origins.json"

ORIGINS = ("official", "hermes", "user", "unknown")
MANUAL_ORIGINS = ("official", "hermes", "user")
MAX_ENTRIES = 500
MAX_NOTE = 200
MAX_KEY = 300

_write_lock = threading.Lock()
_overlay_cache = {"key": None, "entries": {}, "error": None}


class OriginError(ValueError):
    """Invalid manual classification request."""


def _stat_key(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _cached_json(path: Path, mtime: float, size: int):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ``lru_cache`` is not used for the ledger: we return a nested dict that callers
# only read, and keeping the cache key explicit makes the invalidation obvious.
def _bundled_cached(mtime: float, size: int):
    names = set()
    path = SKILLS_DIR / ".bundled_manifest"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.split(":", 1)[0].strip()
            if name:
                names.add(name)
    except OSError:
        return names
    return names


_bundled_cache = {"key": None, "values": set()}


def bundled_names():
    path = SKILLS_DIR / ".bundled_manifest"
    key = _stat_key(path)
    if _bundled_cache["key"] != key:
        _bundled_cache["values"] = _bundled_cached(*key)
        _bundled_cache["key"] = key
    return _bundled_cache["values"]


def _hub_cached(mtime: float, size: int):
    data = _cached_json(SKILLS_DIR / ".hub" / "lock.json", mtime, size) or {}
    installed = data.get("installed")
    if not isinstance(installed, dict):
        return {}
    out = {}
    for name, meta in installed.items():
        if not isinstance(meta, dict):
            continue
        out[name] = {
            "identifier": meta.get("identifier"),
            "source": meta.get("source"),
            "installed_at": meta.get("installed_at"),
        }
    return out


_hub_cache = {"key": None, "values": {}}


def hub_installed():
    path = SKILLS_DIR / ".hub" / "lock.json"
    key = _stat_key(path)
    if _hub_cache["key"] != key:
        _hub_cache["values"] = _hub_cached(*key)
        _hub_cache["key"] = key
    return _hub_cache["values"]


def _ledger_cached(mtime: float, size: int):
    """name -> {create: {...} | None, events: int, actors: [str]}"""
    out = {}
    path = SKILLS_DIR / ".curator_ledger.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        name = event.get("skill")
        if not name:
            continue
        record = out.setdefault(name, {"create": None, "events": 0, "actors": set()})
        record["events"] += 1
        actor = event.get("actor") or "unknown"
        record["actors"].add(actor)
        if event.get("action") == "create":
            ts = event.get("ts")
            current = record["create"]
            if current is None or (ts and ts < current.get("created_at", "")):
                record["create"] = {
                    "actor": actor,
                    "created_at": ts,
                    "session_id": (event.get("evidence") or {}).get("session_id"),
                }
    for record in out.values():
        record["actors"] = sorted(record["actors"])
    return out


_ledger_cache = {"key": None, "values": {}}


def ledger_index():
    path = SKILLS_DIR / ".curator_ledger.jsonl"
    key = _stat_key(path)
    if _ledger_cache["key"] != key:
        _ledger_cache["values"] = _ledger_cached(*key)
        _ledger_cache["key"] = key
    return _ledger_cache["values"]


def _read_entries():
    """Fresh read used before a write; refuses to overwrite a broken file."""
    if not OVERLAY_PATH.exists():
        return {}
    try:
        raw = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OriginError(f"覆盖记录损坏，拒绝写入：{exc}") from exc
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise OriginError("覆盖记录格式不正确，拒绝写入")
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _overlay():
    key = _stat_key(OVERLAY_PATH)
    if _overlay_cache["key"] == key:
        return _overlay_cache["entries"], _overlay_cache["error"]
    entries, error = {}, None
    if key[1] >= 0:
        try:
            raw = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
            value = raw.get("entries") if isinstance(raw, dict) else None
            if isinstance(value, dict):
                entries = {k: v for k, v in value.items() if isinstance(v, dict)}
            else:
                error = "覆盖记录格式不正确"
        except (OSError, ValueError) as exc:
            error = f"覆盖记录读取失败：{exc}"
    _overlay_cache.update(key=key, entries=entries, error=error)
    return entries, error


def overlay_error():
    return _overlay()[1]


def overlay_key(item):
    rel = (item.get("relative_path") or "").strip()
    if rel:
        return rel
    return "name:" + (item.get("name") or "")


def manual_entry(item):
    entries, _ = _overlay()
    return entries.get(overlay_key(item))


def _result(origin, rule, evidence, overridden=False):
    return {
        "origin": origin,
        "rule": rule,
        "evidence": evidence,
        "overridden": bool(overridden),
    }


def origin_for(item):
    """Classify one registry entry. Never raises; degrades to inference."""
    name = item.get("name") or ""
    manual = manual_entry(item)
    if manual and manual.get("origin") in MANUAL_ORIGINS:
        return _result(
            manual["origin"],
            "manual",
            {"decided_at": manual.get("decided_at"), "note": manual.get("note") or ""},
            overridden=True,
        )

    ledger = ledger_index().get(name) or {}
    create = ledger.get("create")
    source_type = item.get("source_type") or ""

    if source_type in ("builtin", "builtin-shadow") or name in bundled_names():
        return _result(
            "official",
            "official-bundled",
            {
                "manifest": name in bundled_names(),
                "source_type": source_type or None,
                "ledger_create": create,
            },
        )

    hub = hub_installed().get(name)
    if hub:
        return _result("official", "official-hub", hub)

    if create:
        actor = create.get("actor") or ""
        session = create.get("session_id") or ""
        if actor == "curator":
            return _result("hermes", "hermes-curator", create)
        if actor == "agent" and session.startswith("cron_"):
            return _result("hermes", "hermes-cron", create)
        if actor == "agent":
            return _result("user", "user-agent", create)

    return _result(
        "unknown",
        "no-evidence",
        {"events": ledger.get("events", 0), "actors": ledger.get("actors") or []},
    )


def _clean_note(note: str) -> str:
    text = "".join(ch for ch in str(note or "") if ch.isprintable())
    text = " ".join(text.split())
    if len(text) > MAX_NOTE:
        raise OriginError(f"备注不能超过 {MAX_NOTE} 个字符")
    return text


def _write(entries):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    payload = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": entries,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".skill-origins-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OVERLAY_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def set_override(key: str, origin: str, note: str = ""):
    if origin not in MANUAL_ORIGINS:
        raise OriginError("只能手工指定为 official / hermes / user")
    key = (key or "").strip()
    if not key or len(key) > MAX_KEY:
        raise OriginError("技能标识无效")
    cleaned = _clean_note(note)
    with _write_lock:
        entries = _read_entries()
        if key not in entries and len(entries) >= MAX_ENTRIES:
            raise OriginError(f"覆盖记录已达上限 {MAX_ENTRIES} 条")
        entries[key] = {
            "origin": origin,
            "decided_by": "manual",
            "decided_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": cleaned,
        }
        _write(entries)
    return entries[key]


def clear_override(key: str) -> bool:
    key = (key or "").strip()
    with _write_lock:
        entries = _read_entries()
        if key not in entries:
            return False
        del entries[key]
        _write(entries)
    return True


def orphans(items):
    """Manual keys that no longer match a skill in the registry."""
    entries, _ = _overlay()
    current = {overlay_key(item) for item in items}
    return sorted(k for k in entries if k not in current)
