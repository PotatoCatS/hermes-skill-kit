"""HTTP API over the skill-management artifacts.

Read routes stay GET-only. The approval center adds a small, fixed set of
POST routes that call the existing pipeline scripts through app/approvals.py;
there is no endpoint that accepts a command, path, or arbitrary flag.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import approvals, audit_log, auth, data, favorites, origins
from .safety import UnsafePath

router = APIRouter()


class SetupBody(BaseModel):
    password: str
    confirm: str


class LoginBody(BaseModel):
    password: str


class ApproveBody(BaseModel):
    quote: str


class RejectBody(BaseModel):
    reason: str


class ApplyBody(BaseModel):
    dry_run: bool = True
    confirmation: str = ""


class RollbackBody(BaseModel):
    confirmation: str = ""


class OriginBody(BaseModel):
    origin: str
    note: str = ""


class FavoriteBody(BaseModel):
    note: str = ""


class MoveBody(BaseModel):
    direction: str


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else None) or "local"


def _approval_error(exc: approvals.ApprovalError) -> HTTPException:
    detail = getattr(exc, "detail", str(exc))
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.get("/health")
def health():
    return {
        "status": "ok",
        "service": "hermes-skill-dashboard",
        "mode": "local-approval",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.get("/auth/status")
def auth_status(request: Request):
    session = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
    payload = {
        "setup_required": auth.setup_required(),
        "authenticated": bool(session),
        "user": "admin" if session else None,
    }
    if session:
        payload["csrf_token"] = session.get("csrf")
        payload["expires_at"] = session.get("exp")
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/setup")
def auth_setup(body: SetupBody, request: Request):
    try:
        token, info = auth.setup(body.password, body.confirm, _client_ip(request))
    except auth.RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    response = JSONResponse({"ok": True, "user": info["user"], "csrf_token": info["csrf_token"]})
    auth.set_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request):
    try:
        token = auth.login(body.password, _client_ip(request))
    except auth.RateLimited as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    csrf = auth.csrf_from_token(token)
    response = JSONResponse({"ok": True, "user": "admin", "csrf_token": csrf})
    auth.set_cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True})
    auth.clear_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/dashboard")
def dashboard():
    try:
        return data.dashboard()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="registry/skills.json 不存在")


@router.get("/skills")
def list_skills(
    q: str = "",
    category: str = "",
    status: str = "",
    source: str = "",
    origin: str = "",
    sort: str = "name",
    order: str = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    fav_keys = favorites.keys()
    items = []
    for entry in data.skills():
        summary = data.skill_summary(entry)
        summary["favorite"] = favorites.skill_key(entry) in fav_keys
        items.append(summary)

    ql = q.strip().lower()
    if ql:
        items = [
            x
            for x in items
            if ql in (x.get("name") or "").lower()
            or ql in (x.get("description") or "").lower()
            or any(ql in (t or "").lower() for t in x.get("tags") or [])
        ]
    if category:
        items = [x for x in items if (x.get("category") or "") == category]
    if status:
        items = [x for x in items if (x.get("load_status") or "") == status]
    if source:
        items = [x for x in items if (x.get("source_type") or "") == source]
    if origin:
        items = [x for x in items if (x.get("origin") or "") == origin]

    allowed_sort = {
        "name", "category", "source_type", "origin", "load_status", "total_bytes", "file_count",
    }
    sort_key = sort if sort in allowed_sort else "name"
    items.sort(key=lambda x: (x.get(sort_key) is None, x.get(sort_key)), reverse=(order == "desc"))

    total = len(items)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": items[start : start + page_size],
        "facets": {
            "categories": sorted({x.get("category") or "未分类" for x in (data.skill_summary(i) for i in data.skills())}),
            "statuses": sorted({x.get("load_status") or "unknown" for x in (data.skill_summary(i) for i in data.skills())}),
            "sources": sorted({x.get("source_type") or "unknown" for x in (data.skill_summary(i) for i in data.skills())}),
            "origins": sorted({x.get("origin") or "unknown" for x in (data.skill_summary(i) for i in data.skills())}),
        },
    }


@router.get("/skills/{name}")
def skill_detail(name: str):
    detail = data.skill_detail(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {name}")
    return detail


def _one_skill(name: str):
    matches = [x for x in data.skills() if x.get("name") == name]
    if not matches:
        raise HTTPException(status_code=404, detail=f"未找到 Skill: {name}")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"存在 {len(matches)} 个同名 Skill，无法确定要修改哪一个",
        )
    return matches[0]


@router.put("/skills/{name}/origin")
def set_skill_origin(name: str, body: OriginBody):
    item = _one_skill(name)
    key = origins.overlay_key(item)
    try:
        entry = origins.set_override(key, body.origin, body.note)
    except origins.OriginError as exc:
        audit_log.log_action("origin.set", f"{name} -> {body.origin} 拒绝: {exc}", ok=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log.log_action("origin.set", f"{name} -> {body.origin} ({key})")
    return {"ok": True, "key": key, "entry": entry, "item": data.skill_summary(item)}


@router.delete("/skills/{name}/origin")
def clear_skill_origin(name: str):
    item = _one_skill(name)
    key = origins.overlay_key(item)
    try:
        removed = origins.clear_override(key)
    except origins.OriginError as exc:
        audit_log.log_action("origin.clear", f"{name} 拒绝: {exc}", ok=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="该 Skill 没有手工指定的产生方式")
    audit_log.log_action("origin.clear", f"{name} ({key})")
    return {"ok": True, "item": data.skill_summary(item)}


@router.get("/favorites")
def list_favorites():
    return data.favorites_view()


@router.put("/favorites/{name}")
def add_favorite(name: str, body: FavoriteBody):
    item = _one_skill(name)
    key = favorites.skill_key(item)
    try:
        entry, created = favorites.save(key, body.note)
    except favorites.FavoriteFull as exc:
        audit_log.log_action("favorite.add", f"{name} 拒绝: {exc}", ok=False)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except favorites.FavoriteError as exc:
        audit_log.log_action("favorite.add", f"{name} 拒绝: {exc}", ok=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_log.log_action("favorite.add" if created else "favorite.note", f"{name} ({key})")
    return {
        "ok": True,
        "created": created,
        "key": key,
        "entry": entry,
        "favorites": data.favorites_view(),
    }


@router.delete("/favorites/{name}")
def remove_favorite(name: str):
    item = _one_skill(name)
    key = favorites.skill_key(item)
    try:
        removed = favorites.remove(key)
    except favorites.FavoriteError as exc:
        audit_log.log_action("favorite.remove", f"{name} 拒绝: {exc}", ok=False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="该 Skill 不在常用列表里")
    audit_log.log_action("favorite.remove", f"{name} ({key})")
    return {"ok": True, "favorites": data.favorites_view()}


@router.delete("/favorites")
def remove_favorite_by_key(key: str = Query("", max_length=300)):
    """Remove a stored entry by its key; used when the skill is gone."""
    key = key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 key 参数")
    try:
        removed = favorites.remove(key)
    except favorites.FavoriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="该条记录不在常用列表里")
    audit_log.log_action("favorite.remove", f"key={key}")
    return {"ok": True, "favorites": data.favorites_view()}


@router.post("/favorites/{name}/move")
def move_favorite(name: str, body: MoveBody):
    item = _one_skill(name)
    key = favorites.skill_key(item)
    try:
        moved = favorites.move(key, body.direction)
    except favorites.FavoriteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if moved:
        audit_log.log_action("favorite.move", f"{name} {body.direction} ({key})")
    return {"ok": True, "moved": moved, "favorites": data.favorites_view()}


@router.get("/reports")
def reports(kind: str = "daily"):
    try:
        return {"kind": kind, "items": data.report_list(kind)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/reports/content")
def report_content(kind: str = "daily", name: str = ""):
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name 参数")
    try:
        return {"kind": kind, "name": name, "content": data.report_content(kind, name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="报告不存在")
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc))


SECURITY_RANK = {"High": 3, "Medium": 2, "Low": 1}
UPDATE_RANK = {"未知": 3, "已确认": 2, "不适用": 1}


@router.get("/audits")
def audits():
    summary = data.latest_audit_summary()
    return {"summary": summary, "meta": data.audit_meta(), "reports": data.audit_reports()}


@router.get("/audits/skills")
def audit_skills(
    q: str = "",
    grade: str = "",
    level: str = "",
    update: str = "",
    dup: str = "",
    sort: str = "quality",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    all_rows = data.audit_rows()
    rows = list(all_rows)

    ql = q.strip().lower()
    if ql:
        rows = [
            r
            for r in rows
            if ql in (r.get("name") or "").lower()
            or ql in (r.get("category") or "").lower()
            or ql in (r.get("upstream") or "").lower()
            or ql in (r.get("security_summary") or "").lower()
            or ql in (r.get("update_note") or "").lower()
        ]
    if grade:
        rows = [r for r in rows if (r.get("quality_grade") or "") == grade]
    if level:
        rows = [r for r in rows if (r.get("security_level") or "") == level]
    if update:
        rows = [r for r in rows if (r.get("update_conclusion") or "") == update]
    if dup in ("high", "any", "none"):
        if dup == "high":
            rows = [
                r
                for r in rows
                if any((x.get("classification") == "High Overlap") for x in r.get("duplication") or [])
            ]
        elif dup == "any":
            rows = [r for r in rows if r.get("duplication")]
        else:
            rows = [r for r in rows if not r.get("duplication")]

    allowed_sort = {"name", "category", "quality", "security", "update"}
    sort_key = sort if sort in allowed_sort else "quality"

    def rank(r):
        if sort_key == "quality":
            value = r.get("quality_total")
            return value if value is not None else -1
        if sort_key == "security":
            return SECURITY_RANK.get(r.get("security_level"), 0)
        if sort_key == "update":
            return UPDATE_RANK.get(r.get("update_conclusion"), 0)
        if sort_key == "category":
            return r.get("category") or "未分类"
        return r.get("name") or ""

    rows.sort(key=rank, reverse=(order == "desc"))

    total = len(rows)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": rows[start : start + page_size],
        "facets": {
            "grades": sorted({r.get("quality_grade") for r in all_rows if r.get("quality_grade")}),
            "levels": [
                x
                for x in ("High", "Medium", "Low")
                if any(r.get("security_level") == x for r in all_rows)
            ],
            "updates": [
                x
                for x in ("已确认", "未知", "不适用")
                if any(r.get("update_conclusion") == x for r in all_rows)
            ],
            "categories": sorted({r.get("category") or "未分类" for r in all_rows}),
        },
        "meta": data.audit_meta(),
    }


@router.get("/audits/history")
def audit_history():
    return {"items": data.audit_history(), "meta": data.audit_meta()}


@router.get("/audits/content")
def audit_content(kind: str = "quality", name: str = ""):
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name 参数")
    try:
        return {"kind": kind, "name": name, "content": data.audit_content(kind, name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="审计报告不存在")
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/proposals")
def proposals():
    raw = data.proposals()
    cleaned = {
        status: [
            n
            for n in names
            if not n.startswith(".") and not n.upper().startswith(("TEMPLATE", "README"))
        ]
        for status, names in raw.items()
    }
    return {
        "counts": {k: len(v) for k, v in cleaned.items()},
        "items": cleaned,
    }


@router.get("/proposals/content")
def proposal_content(status: str = "pending", name: str = ""):
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name 参数")
    try:
        return {"status": status, "name": name, "content": data.proposal_content(status, name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="提案不存在")
    except UnsafePath as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------- approval center ----------------

@router.get("/approvals/summary")
def approval_summary():
    try:
        listing = approvals.list_proposals()
        change_items = approvals.changes()
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None
    return {
        "counts": listing.get("counts") or {},
        "errors": listing.get("errors") or [],
        "changes": len(change_items),
        "latest_change": change_items[0] if change_items else None,
    }


@router.get("/approvals/proposals")
def approval_proposals():
    try:
        return approvals.list_proposals()
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.get("/approvals/proposals/{proposal_id}")
def approval_proposal(proposal_id: str):
    try:
        return approvals.proposal_detail(proposal_id)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.post("/approvals/proposals/{proposal_id}/approve")
def approval_approve(proposal_id: str, body: ApproveBody):
    try:
        return approvals.approve(proposal_id, body.quote)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.post("/approvals/proposals/{proposal_id}/reject")
def approval_reject(proposal_id: str, body: RejectBody):
    try:
        return approvals.reject(proposal_id, body.reason)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.post("/approvals/proposals/{proposal_id}/apply")
def approval_apply(proposal_id: str, body: ApplyBody):
    try:
        return approvals.apply_proposal(proposal_id, body.dry_run, body.confirmation)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.get("/approvals/changes")
def approval_changes():
    try:
        return {"items": approvals.changes()}
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.get("/approvals/changes/{change_id}")
def approval_change(change_id: str):
    try:
        return approvals.change_detail(change_id)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.post("/approvals/changes/{change_id}/rollback")
def approval_rollback(change_id: str, body: RollbackBody):
    try:
        return approvals.rollback_change(change_id, body.confirmation)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None


@router.get("/approvals/logs")
def approval_logs(name: str = "change"):
    try:
        return approvals.logs(name)
    except approvals.ApprovalError as exc:
        raise _approval_error(exc) from None
