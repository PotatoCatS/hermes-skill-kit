"""Dashboard-local list of commonly used skills.

The list lives in ``data/skill-favorites.json`` next to this package and keeps
insertion order, which is also the dashboard display order. It is capped at 12
entries with a 200-character note per entry.

Keys are a skill's ``relative_path`` (falling back to ``name:<skill>``), the same
identifier the origin overlay uses, so a registry rescan does not lose entries.

Only this file is written. Skills, the registry and the Hermes configuration are
never touched.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SKD_DATA_DIR", str(DASHBOARD_DIR / "data")))
FAVORITES_PATH = DATA_DIR / "skill-favorites.json"

MAX_ENTRIES = 12
MAX_NOTE = 200
MAX_KEY = 300

_write_lock = threading.Lock()
_cache = {"key": None, "entries": [], "error": None}


class FavoriteError(ValueError):
    """Invalid favorite request."""


class FavoriteFull(FavoriteError):
    """The list already holds MAX_ENTRIES entries."""


def skill_key(item):
    rel = (item.get("relative_path") or "").strip()
    if rel:
        return rel
    return "name:" + (item.get("name") or "")


def _stat_key(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _sanitize(raw_entries):
    out = []
    seen = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key or key in seen or len(key) > MAX_KEY:
            continue
        seen.add(key)
        note = "".join(ch for ch in str(entry.get("note") or "") if ch.isprintable())
        out.append({
            "key": key,
            "note": note[:MAX_NOTE],
            "added_at": str(entry.get("added_at") or ""),
        })
        if len(out) >= MAX_ENTRIES:
            break
    return out


def _read_fresh():
    """Fresh read used before a write; refuses to overwrite a broken file."""
    if not FAVORITES_PATH.exists():
        return []
    try:
        raw = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FavoriteError(f"常用列表损坏，拒绝写入：{exc}") from exc
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise FavoriteError("常用列表格式不正确，拒绝写入")
    return _sanitize(entries)


def _loaded():
    key = _stat_key(FAVORITES_PATH)
    if _cache["key"] == key:
        return _cache["entries"], _cache["error"]
    entries, error = [], None
    if key[1] >= 0:
        try:
            raw = json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
            value = raw.get("entries") if isinstance(raw, dict) else None
            if isinstance(value, list):
                entries = _sanitize(value)
            else:
                error = "常用列表格式不正确"
        except (OSError, ValueError) as exc:
            error = f"常用列表读取失败：{exc}"
    _cache.update(key=key, entries=entries, error=error)
    return entries, error


def entries():
    return list(_loaded()[0])


def error():
    return _loaded()[1]


def keys():
    return {entry["key"] for entry in _loaded()[0]}


def entry(key):
    for item in _loaded()[0]:
        if item["key"] == key:
            return dict(item)
    return None


def _clean_note(note: str) -> str:
    text = "".join(ch for ch in str(note or "") if ch.isprintable())
    text = " ".join(text.split())
    if len(text) > MAX_NOTE:
        raise FavoriteError(f"备注不能超过 {MAX_NOTE} 个字符")
    return text


def _write(entries_to_write):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    payload = {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": entries_to_write,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".skill-favorites-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, FAVORITES_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def save(key: str, note: str = ""):
    """Add a skill to the list, or update its note in place.

    Returns ``(entry, created)``. Raises ``FavoriteFull`` when adding would
    exceed MAX_ENTRIES.
    """
    key = (key or "").strip()
    if not key or len(key) > MAX_KEY:
        raise FavoriteError("技能标识无效")
    cleaned = _clean_note(note)
    with _write_lock:
        current = _read_fresh()
        created = True
        for item in current:
            if item["key"] == key:
                item["note"] = cleaned
                created = False
                break
        else:
            if len(current) >= MAX_ENTRIES:
                raise FavoriteFull(f"常用列表最多 {MAX_ENTRIES} 个，请先移除一个")
            current.append({
                "key": key,
                "note": cleaned,
                "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        _write(current)
    return entry(key), created


def remove(key: str) -> bool:
    key = (key or "").strip()
    with _write_lock:
        current = _read_fresh()
        kept = [item for item in current if item["key"] != key]
        if len(kept) == len(current):
            return False
        _write(kept)
    return True


def move(key: str, direction: str) -> bool:
    """Move one entry up/down. Returns False at the boundary or if unknown."""
    if direction not in ("up", "down"):
        raise FavoriteError("direction 只能是 up 或 down")
    key = (key or "").strip()
    with _write_lock:
        current = _read_fresh()
        index = next((i for i, item in enumerate(current) if item["key"] == key), None)
        if index is None:
            return False
        target = index - 1 if direction == "up" else index + 1
        if target < 0 or target >= len(current):
            return False
        current[index], current[target] = current[target], current[index]
        _write(current)
    return True


def missing_keys(current_keys):
    """Favorites whose skill is no longer in the registry (kept, not dropped)."""
    return [entry["key"] for entry in _loaded()[0] if entry["key"] not in current_keys]
