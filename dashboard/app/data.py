"""Read-only data access over the existing skill-management artifacts."""
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

from . import favorites, origins
from .safety import BASE, SKILLS_DIR, read_text, skill_file

REGISTRY = BASE / "registry" / "skills.json"


def _load_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=4)
def _registry_cached(mtime: float, size: int):
    return _load_json(REGISTRY)


def registry():
    stat = REGISTRY.stat()
    return _registry_cached(stat.st_mtime, stat.st_size)


def skills():
    data = registry().get("skills", {})
    return list(data.values()) if isinstance(data, dict) else data


def scan_meta():
    return registry().get("scan_meta", {})


def _md_files(folder: Path):
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.md")), key=lambda p: p.name, reverse=True)


def proposals():
    """Proposal files per status, skipping placeholders (.gitkeep, TEMPLATE, README)."""
    result = {}
    for status in ("pending", "approved", "rejected"):
        folder = BASE / "proposals" / status
        files = []
        if folder.is_dir():
            files = sorted(
                p.name
                for p in folder.iterdir()
                if p.is_file()
                and not p.name.startswith(".")
                and not p.name.upper().startswith(("TEMPLATE", "README"))
            )
        result[status] = files
    return result


AUDITS_DIR = BASE / "audits"


@lru_cache(maxsize=8)
def _json_cached(path: str, mtime: float, size: int):
    return _load_json(Path(path))


def _read_json(path: Path):
    stat = path.stat()
    return _json_cached(str(path), stat.st_mtime, stat.st_size)


def audit_summary_files():
    return sorted(AUDITS_DIR.glob("summary-*.json"), key=lambda p: p.name, reverse=True)


def latest_audit_summary():
    files = audit_summary_files()
    if not files:
        return None
    return {"name": files[0].name, "data": _read_json(files[0])}


def audit_history():
    """All audit summaries, newest first, with only the fields needed for comparison."""
    out = []
    for path in audit_summary_files()[:50]:
        data = _read_json(path)
        baseline = data.get("baseline") or {}
        out.append(
            {
                "name": path.name,
                "audit_date": data.get("audit_date"),
                "generated_at": data.get("generated_at"),
                "baseline_scan_time": baseline.get("scan_time"),
                "baseline_skill_count": baseline.get("skill_count"),
                "scope": data.get("scope") or {},
                "totals": data.get("totals") or {},
                "read_only": data.get("read_only"),
                "registry_modified": data.get("registry_modified"),
                "skills_modified": data.get("skills_modified"),
            }
        )
    return out


def audit_rows():
    summary = latest_audit_summary()
    if not summary:
        return []
    rows = []
    for item in summary["data"].get("skills", []):
        quality = item.get("quality") or {}
        security = item.get("security") or {}
        updates = item.get("updates") or {}
        rows.append(
            {
                "audit_date": summary["data"].get("audit_date"),
                "name": item.get("name"),
                "category": item.get("category"),
                "source_type": item.get("source_type"),
                "load_status": item.get("load_status"),
                "quality_total": quality.get("total"),
                "quality_grade": quality.get("grade"),
                "quality_dimensions": quality.get("dimensions") or {},
                "security_level": security.get("level"),
                "security_summary": security.get("summary"),
                "update_conclusion": updates.get("conclusion"),
                "upstream": updates.get("upstream"),
                "update_note": updates.get("note"),
                "duplication": item.get("duplication") or [],
            }
        )
    return rows


def audit_meta():
    summary = latest_audit_summary()
    if not summary:
        return None
    data = summary["data"]
    return {
        "name": summary["name"],
        "audit_date": data.get("audit_date"),
        "generated_at": data.get("generated_at"),
        "baseline": data.get("baseline") or {},
        "scope": data.get("scope") or {},
        "read_only": data.get("read_only"),
        "registry_modified": data.get("registry_modified"),
        "skills_modified": data.get("skills_modified"),
    }


def skill_audit(name: str):
    for row in audit_rows():
        if row.get("name") == name:
            return row
    return None


def favorites_view(items=None):
    """常用技能 joined with the registry, in stored order.

    Entries whose skill disappeared stay in the list and are reported with
    ``missing: true`` instead of being dropped silently.
    """
    source = list(items if items is not None else skills())
    index = {}
    for item in source:
        index.setdefault(favorites.skill_key(item), item)
    out = []
    for entry in favorites.entries():
        item = index.get(entry["key"])
        out.append({
            "key": entry["key"],
            "note": entry.get("note") or "",
            "added_at": entry.get("added_at") or "",
            "missing": item is None,
            "skill": skill_summary(item) if item is not None else None,
        })
    return {"items": out, "limit": favorites.MAX_ENTRIES, "error": favorites.error()}


def dashboard():
    items = skills()
    meta = scan_meta()
    roots = meta.get("roots", {})
    default_root = roots.get("default_skills_dir", {})
    by_category = Counter(x.get("category") or "未分类" for x in items)
    by_source = Counter(x.get("source_type") or "unknown" for x in items)
    by_status = Counter(x.get("load_status") or "unknown" for x in items)
    by_origin = Counter(origins.origin_for(x)["origin"] for x in items)
    compare = sorted((BASE / "registry").glob("compare-report-*.md"))
    daily = _md_files(BASE / "reports" / "daily")
    weekly = _md_files(BASE / "reports" / "weekly")
    prop = proposals()
    audit = latest_audit_summary()
    return {
        "total": len(items),
        "by_status": dict(by_status),
        "by_category": dict(by_category.most_common()),
        "by_source": dict(by_source.most_common()),
        "by_origin": dict(by_origin.most_common()),
        "origin_orphans": len(origins.orphans(items)),
        "origin_error": origins.overlay_error(),
        "total_bytes": sum(int(x.get("total_bytes") or 0) for x in items),
        "scanned_at": meta.get("scanned_at"),
        "scanner": meta.get("generator"),
        "skills_dir": default_root.get("path"),
        "external_dirs": roots.get("external_dirs", {}).get("status"),
        "archived": roots.get("curator_archive", {}).get("archived_skill_count", 0),
        "registry_errors": len(registry().get("errors") or []),
        "collisions": len(registry().get("name_collisions") or []),
        "latest_compare": compare[-1].name if compare else None,
        "latest_audit": audit,
        "reports": {
            "daily": len(daily),
            "weekly": len(weekly),
            "compare": len(compare),
            "latest_daily": daily[0].name if daily else None,
            "latest_weekly": weekly[0].name if weekly else None,
        },
        "proposals": {k: len(v) for k, v in prop.items()},
        "favorites": favorites_view(items),
    }


def skill_summary(item):
    origin = origins.origin_for(item)
    return {
        "name": item.get("name"),
        "description": item.get("description"),
        "version": item.get("version"),
        "category": item.get("category"),
        "source_type": item.get("source_type"),
        "origin": origin["origin"],
        "origin_rule": origin["rule"],
        "origin_evidence": origin["evidence"],
        "origin_overridden": origin["overridden"],
        "load_status": item.get("load_status"),
        "platforms": item.get("platforms", []),
        "file_count": item.get("file_count"),
        "total_bytes": item.get("total_bytes"),
        "sha256": item.get("sha256"),
        "relative_path": item.get("relative_path"),
        "last_scanned": item.get("last_scanned"),
        "tags": item.get("tags", []),
    }


def skill_detail(name: str):
    match = next((x for x in skills() if x.get("name") == name), None)
    if match is None:
        return None
    detail = skill_summary(match)
    detail["origin_detail"] = {
        **origins.origin_for(match),
        "key": origins.overlay_key(match),
        "manual": origins.manual_entry(match),
    }
    fav_key = favorites.skill_key(match)
    fav = favorites.entry(fav_key)
    detail["favorite"] = {
        "key": fav_key,
        "on": fav is not None,
        "note": (fav or {}).get("note", ""),
    }
    detail["files"] = match.get("files", [])
    detail["path"] = match.get("path")
    detail["dependencies"] = match.get("dependencies", [])
    detail["author"] = match.get("author")
    detail["license"] = match.get("license")
    try:
        detail["skill_md"] = skill_file(match.get("path", ""), "SKILL.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except (OSError, ValueError) as exc:
        detail["skill_md"] = f"（无法读取 SKILL.md：{exc}）"
    detail["audit"] = skill_audit(name)
    return detail


REPORT_KINDS = {
    "daily": ("reports", "reports/daily"),
    "weekly": ("reports", "reports/weekly"),
    "monthly": ("reports", "reports/monthly"),
    "compare": ("registry", "registry"),
}


def report_list(kind: str):
    if kind not in REPORT_KINDS:
        raise ValueError(f"unknown report kind: {kind}")
    _, rel = REPORT_KINDS[kind]
    folder = BASE / rel
    if not folder.is_dir():
        return []
    pattern = "compare-report-*.md" if kind == "compare" else "*.md"
    return sorted((p.name for p in folder.glob(pattern)), reverse=True)


def report_content(kind: str, name: str):
    if kind not in REPORT_KINDS:
        raise ValueError(f"unknown report kind: {kind}")
    root_key, rel = REPORT_KINDS[kind]
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid report name")
    inner = name if rel == "registry" else rel[len("reports/"):] + "/" + name
    return read_text(root_key, inner)


AUDIT_KINDS = ("quality", "security", "duplication", "updates")


def audit_reports():
    folder = BASE / "audits"
    out = {}
    for sub in AUDIT_KINDS:
        out[sub] = (
            sorted((p.name for p in (folder / sub).glob("*.md")), reverse=True)
            if (folder / sub).is_dir()
            else []
        )
    return out


def audit_content(kind: str, name: str):
    if kind not in AUDIT_KINDS:
        raise ValueError(f"unknown audit kind: {kind}")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid file name")
    return read_text("audits", f"{kind}/{name}")


def proposal_content(status: str, name: str):
    if status not in ("pending", "approved", "rejected"):
        raise ValueError("invalid status")
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid file name")
    return read_text("proposals", f"{status}/{name}")
