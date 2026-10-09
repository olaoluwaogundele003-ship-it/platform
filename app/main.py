"""Platform monolith — FastAPI app serving API + UI in one process."""
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app import models
from app.auth import (
    get_current_user, require_member, require_admin,
    hash_password, verify_password, create_token, user_from_token,
)
from app.rules import evaluate
from app import ai as ai_mod
from app.workflows import (
    push_inbox, post_message, create_offers_for_task,
    fire_event, execute_action, compute_view_rows,
)
from app.agents_logic import run_agent, confirm_agent_run, analyst_query
from app.scheduler_loop import start_loop, normalize_schedule, run_schedule_once
from app.seed import seed, ensure_demo_credentials, migrate

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# ephemeral presence (typing indicators) — in-memory with TTL, fine for monolith
_typing = {}  # (group_id, member_id) -> {"name": str, "until": float}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        seed(db)
        ensure_demo_credentials(db)
        migrate(db)
    finally:
        db.close()
    start_loop()
    yield


app = FastAPI(title="Platform — group chat that organizes itself", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


def utcnow():
    return datetime.now(timezone.utc)


# ---------- helpers ----------
def g_or_404(db, gid):
    g = db.query(models.Group).filter_by(id=gid).first()
    if not g:
        raise HTTPException(404, "group not found")
    return g


def m_or_404(db, gid, mid):
    m = db.query(models.Membership).filter_by(id=mid, group_id=gid).first()
    if not m:
        raise HTTPException(404, "member not found in group")
    return m


def me_in(db, gid, user):
    m = db.query(models.Membership).filter_by(group_id=gid, user_id=user.id).first()
    if not m:
        raise HTTPException(403, "you are not a member of this group")
    return m


# ---------- meta ----------
@app.get("/api/health")
def health():
    return {"ok": True, "time": utcnow().isoformat(), "app": "platform-monolith"}


@app.get("/api/ai-status")
def ai_status():
    return ai_mod.ai_status()


# ---------- auth (standard) ----------
@app.post("/api/auth/register")
def register(body: dict, db: Session = Depends(get_db)):
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    name = (body.get("name") or username).strip()
    email = (body.get("email") or f"{username}@platform.local").strip()
    if not username or len(username) < 3:
        raise HTTPException(400, "username required (min 3 chars)")
    if not password or len(password) < 4:
        raise HTTPException(400, "password required (min 4 chars)")
    if db.query(models.User).filter_by(username=username).first():
        raise HTTPException(400, "username taken")
    if db.query(models.User).filter_by(email=email).first():
        raise HTTPException(400, "email already registered")
    u = models.User(name=name, email=email, username=username, password_hash=hash_password(password))
    db.add(u)
    db.commit()
    tok = create_token(db, u.id)
    return {"token": tok, "user": {"id": u.id, "username": username, "name": name}}


@app.post("/api/auth/login")
def login(body: dict, db: Session = Depends(get_db)):
    ident = (body.get("username") or body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    u = db.query(models.User).filter_by(username=ident).first() or \
        db.query(models.User).filter_by(email=body.get("username") or "").first()
    if not u or not u.password_hash or not verify_password(password, u.password_hash):
        raise HTTPException(401, "invalid username or password")
    tok = create_token(db, u.id)
    return {"token": tok, "user": {"id": u.id, "username": u.username, "name": u.name}}


@app.post("/api/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
        or request.headers.get("x-session-token")
    if token:
        row = db.query(models.AuthToken).filter_by(token=token).first()
        if row:
            db.delete(row)
            db.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    groups = db.query(models.Membership).filter_by(user_id=user.id).all()
    return {"id": user.id, "username": user.username, "name": user.name, "email": user.email,
            "groups": [{"group_id": m.group_id, "member_id": m.id, "role": m.role} for m in groups]}


# ---------- users ----------
@app.get("/api/users")
def list_users(q: str = "", user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(models.User)
    if q:
        like = f"%{q}%"
        query = query.filter(models.User.username.like(like) | models.User.name.like(like))
    return [{"id": u.id, "username": u.username, "name": u.name} for u in query.limit(20).all()]


# ---------- groups & members ----------
@app.get("/api/groups")
def list_groups(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member_of = {m.group_id: m for m in db.query(models.Membership).filter_by(user_id=user.id).all()}
    out = []
    for g in db.query(models.Group).order_by(models.Group.id).all():
        if g.id not in member_of:
            continue
        m = member_of[g.id]
        n = db.query(models.Membership).filter_by(group_id=g.id).count()
        out.append({"id": g.id, "name": g.name, "description": g.description,
                    "workspace": g.workspace, "members": n,
                    "my_role": m.role, "my_member_id": m.id})
    return out


@app.post("/api/groups")
def create_group(body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not (body.get("name") or "").strip():
        raise HTTPException(400, "name required")
    g = models.Group(name=body["name"].strip(), description=body.get("description", ""),
                     workspace=body.get("workspace", "Northstar Workspace"), created_by=user.id)
    db.add(g)
    db.flush()
    db.add(models.TableDef(group_id=g.id, key="members", name="Members",
                           description="Group directory (hub).", is_hub=True,
                           columns=[{"name": "name", "label": "Name", "type": "text", "required": True}]))
    # starter tables so a fresh group is usable immediately (still under the cap)
    db.add(models.TableDef(group_id=g.id, key="tasks_log", name="Tasks log",
                           description="Structured outcomes.", columns=[
                               {"name": "title", "label": "Title", "type": "text", "required": True},
                               {"name": "status", "label": "Status", "type": "text"}]))
    db.add(models.Membership(group_id=g.id, user_id=user.id, display_name=user.name,
                             role="admin", tags=[], profile={}))
    # seed an AI analyst agent + welcome content
    db.add(models.AgentDef(group_id=g.id, name="Platform analyst", kind="analyst",
                           description="Answers questions and proposes structured changes.",
                           config={"permissions": ["read", "suggest"]}))
    db.flush()
    post_message(db, g.id, f"Group '{g.name}' created. Invite people with the code {g.invite_code}.", kind="system")
    db.commit()
    return {"id": g.id, "name": g.name, "invite_code": g.invite_code}


@app.post("/api/groups/join")
def join_group(body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "invite code required")
    g = db.query(models.Group).filter_by(invite_code=code).first()
    if not g:
        raise HTTPException(404, "invalid invite code")
    if db.query(models.Membership).filter_by(group_id=g.id, user_id=user.id).first():
        return {"id": g.id, "name": g.name, "joined": False, "reason": "already a member"}
    m = models.Membership(group_id=g.id, user_id=user.id, display_name=user.name, role="member", tags=[], profile={})
    db.add(m)
    db.flush()
    post_message(db, g.id, f"{user.name} joined via invite link.", kind="system")
    fire_event(db, g.id, "member.joined", {"trigger_ref": None, "member_id": m.id, "note": f"{user.name} joined"})
    db.commit()
    return {"id": g.id, "name": g.name, "joined": True, "member_id": m.id}


@app.get("/api/groups/{gid}")
def get_group(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    g = g_or_404(db, gid)
    me = me_in(db, gid, user)
    members = db.query(models.Membership).filter_by(group_id=gid).all()
    tables = db.query(models.TableDef).filter_by(group_id=gid).all()
    return {
        "id": g.id, "name": g.name, "description": g.description,
        "invite_code": g.invite_code if me.role == "admin" else None,
        "my_member_id": me.id, "my_role": me.role,
        "members": [{"id": m.id, "display_name": m.display_name, "role": m.role,
                     "tags": m.tags or [], "profile": m.profile or {},
                     "prefs": (m.prefs or {}) if m.user_id == user.id else {}} for m in members],
        "tables": [{"id": t.id, "key": t.key, "name": t.name, "is_hub": t.is_hub, "columns": t.columns or []} for t in tables],
    }


@app.get("/api/groups/{gid}/invite")
def get_invite(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    g = g_or_404(db, gid)
    me_in(db, gid, user)
    return {"code": g.invite_code, "group": g.name}


@app.post("/api/groups/{gid}/invite/rotate")
def rotate_invite(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    import secrets
    g = g_or_404(db, gid)
    require_admin(gid, user, db)
    g.invite_code = secrets.token_hex(3).upper()
    db.commit()
    return {"code": g.invite_code}


@app.get("/api/groups/{gid}/members")
def list_members(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return [{"id": m.id, "display_name": m.display_name, "role": m.role,
             "tags": m.tags or [], "profile": m.profile or {},
             "joined_at": m.joined_at.isoformat() if m.joined_at else None}
            for m in db.query(models.Membership).filter_by(group_id=gid).all()]


@app.post("/api/groups/{gid}/members")
def add_member(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    # admins add anyone; members may only add themselves edge-case (kept admin-only)
    if me.role != "admin":
        raise HTTPException(403, "admins only — ask an admin or share the invite link")
    target_user = None
    if body.get("username"):
        target_user = db.query(models.User).filter_by(username=body["username"].strip().lower()).first()
        if not target_user:
            raise HTTPException(404, f"no user '{body['username']}' — they need to register first")
        if db.query(models.Membership).filter_by(group_id=gid, user_id=target_user.id).first():
            raise HTTPException(400, "that user is already in the group")
        m = models.Membership(group_id=gid, user_id=target_user.id, display_name=target_user.name,
                              role=body.get("role", "member"), tags=body.get("tags", []),
                              profile=body.get("profile", {}))
    else:
        if not body.get("display_name"):
            raise HTTPException(400, "username or display_name required")
        m = models.Membership(group_id=gid, user_id=None, display_name=body["display_name"],
                              role=body.get("role", "member"), tags=body.get("tags", []),
                              profile=body.get("profile", {}))
    db.add(m)
    db.flush()
    post_message(db, gid, f"{m.display_name} joined the group.", kind="system")
    fire_event(db, gid, "member.joined", {"trigger_ref": None, "member_id": m.id, "note": f"{m.display_name} joined"})
    db.commit()
    return {"id": m.id, "display_name": m.display_name}


@app.patch("/api/groups/{gid}/members/{mid}")
def update_member(gid: int, mid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    m = m_or_404(db, gid, mid)
    if me.role != "admin" and me.id != m.id:
        raise HTTPException(403, "you can only edit your own profile")
    if "display_name" in body:
        m.display_name = body["display_name"]
    if "tags" in body and me.role == "admin":
        m.tags = body["tags"]
    if "role" in body and me.role == "admin":
        if body["role"] not in ("admin", "member"):
            raise HTTPException(400, "role must be admin|member")
        m.role = body["role"]
    if "profile" in body:
        m.profile = {**(m.profile or {}), **body["profile"]}
    if "prefs" in body and isinstance(body["prefs"], dict):
        # paced catch-up and other personal settings: self or admin
        m.prefs = {**(m.prefs or {}), **body["prefs"]}
    db.commit()
    return {"id": m.id, "display_name": m.display_name, "profile": m.profile, "tags": m.tags, "role": m.role, "prefs": m.prefs or {}}


# ---------- schema: tables & records (5-cap) ----------
@app.get("/api/groups/{gid}/tables")
def list_tables(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    out = []
    for t in db.query(models.TableDef).filter_by(group_id=gid).all():
        n = db.query(models.Record).filter_by(table_id=t.id).count()
        out.append({"id": t.id, "key": t.key, "name": t.name, "description": t.description,
                    "is_hub": t.is_hub, "columns": t.columns or [], "rows": n})
    return out


@app.post("/api/groups/{gid}/tables")
def create_table(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(gid, user, db)
    count = db.query(models.TableDef).filter_by(group_id=gid).count()
    if count >= 15:
        raise HTTPException(400, "Fifteen-table cap reached — Platform keeps every group readable in one pass.")
    if not (body.get("name") or "").strip():
        raise HTTPException(400, "name required")
    key = (body.get("key") or body["name"].lower().replace(" ", "_"))[:64]
    if db.query(models.TableDef).filter_by(group_id=gid, key=key).first():
        raise HTTPException(400, "table key already exists in this group")
    t = models.TableDef(group_id=gid, key=key, name=body["name"].strip(),
                        description=body.get("description", ""), columns=body.get("columns", []))
    db.add(t)
    db.commit()
    return {"id": t.id, "key": t.key, "name": t.name}


@app.delete("/api/tables/{tid}")
def delete_table(tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TableDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "table not found")
    require_admin(t.group_id, user, db)
    if t.is_hub:
        raise HTTPException(400, "the members hub cannot be deleted")
    db.query(models.Record).filter_by(table_id=tid).delete()
    db.delete(t)
    db.commit()
    return {"ok": True}


@app.get("/api/tables/{tid}/records")
def list_records(tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TableDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "table not found")
    me_in(db, t.group_id, user)
    rows = []
    for r in db.query(models.Record).filter_by(table_id=tid).order_by(models.Record.id).all():
        mem = db.query(models.Membership).filter_by(id=r.member_ref_id).first() if r.member_ref_id else None
        rows.append({"id": r.id, "data": r.data or {}, "member": mem.display_name if mem else "-",
                     "member_ref_id": r.member_ref_id,
                     "created_at": r.created_at.isoformat() if r.created_at else None})
    return {"table": {"id": t.id, "key": t.key, "name": t.name, "columns": t.columns or []}, "rows": rows}


@app.post("/api/tables/{tid}/records")
def add_record(tid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TableDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "table not found")
    me = me_in(db, t.group_id, user)
    data = body.get("data", {})
    missing = [c["name"] for c in (t.columns or []) if c.get("required") and not data.get(c["name"])]
    if missing:
        raise HTTPException(400, f"Missing required: {', '.join(missing)}")
    ref_profile = {}
    if body.get("member_ref_id"):
        ref_m = db.query(models.Membership).filter_by(id=body["member_ref_id"]).first()
        if ref_m:
            ref_profile = ref_m.profile or {}
    for r in db.query(models.Rule).filter_by(group_id=t.group_id, status="confirmed").all():
        checks = (r.compiled or {}).get("checks", [])
        if not any(c.get("source") in ("submission", "cross") for c in checks):
            continue
        ok, reasons = evaluate(r.compiled, ref_profile, data)
        if not ok:
            raise HTTPException(400, f"Blocked by '{r.name}': " + "; ".join(reasons))
    rec = models.Record(table_id=tid, group_id=t.group_id,
                        member_ref_id=body.get("member_ref_id") or me.id,
                        data=data, created_by_member_id=me.id)
    db.add(rec)
    db.commit()
    return {"id": rec.id, "data": rec.data}


@app.delete("/api/records/{rid}")
def delete_record(rid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.Record).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "record not found")
    me = me_in(db, r.group_id, user)
    if me.role != "admin" and r.created_by_member_id != me.id:
        raise HTTPException(403, "only the author or an admin can delete this")
    db.delete(r)
    db.commit()
    return {"ok": True}


@app.patch("/api/records/{rid}")
def patch_record(rid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.Record).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "record not found")
    me = me_in(db, r.group_id, user)
    if me.role != "admin" and r.created_by_member_id != me.id:
        raise HTTPException(403, "only the author or an admin can edit this")
    if isinstance(body.get("data"), dict):
        r.data = {**(r.data or {}), **body["data"]}
    db.commit()
    return {"id": r.id, "data": r.data}


@app.patch("/api/tables/{tid}")
def patch_table(tid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TableDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "table not found")
    require_admin(t.group_id, user, db)
    if body.get("name"):
        t.name = str(body["name"]).strip()[:120]
    if "description" in body:
        t.description = str(body.get("description") or "")[:500]
    db.commit()
    return {"id": t.id, "name": t.name}


# ---------- rules ----------
@app.get("/api/groups/{gid}/rules")
def list_rules(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return [{"id": r.id, "name": r.name, "natural_language": r.natural_language,
             "compiled": r.compiled or {}, "explanation": r.explanation,
             "status": r.status, "scope": r.scope, "ai_model": r.ai_model}
            for r in db.query(models.Rule).filter_by(group_id=gid).order_by(models.Rule.id).all()]


@app.post("/api/groups/{gid}/rules/compile")
def compile_rule(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    nl = (body.get("natural_language") or "").strip()
    if not nl:
        raise HTTPException(400, "natural_language required")
    tables = db.query(models.TableDef).filter_by(group_id=gid).all()
    snap = ", ".join(f"{t.key}({len(t.columns or [])} cols)" for t in tables)
    compiled, explanation, model_used = ai_mod.ai_compile_rule(nl, snap)
    return {"compiled": compiled, "explanation": explanation, "model": model_used,
            "warning": "Review carefully — this becomes the single deterministic interpretation." if compiled.get("needs_review") else ""}


@app.post("/api/groups/{gid}/rules")
def create_rule(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = require_admin(gid, user, db)
    nl = (body.get("natural_language") or "").strip()
    if not nl or not body.get("name"):
        raise HTTPException(400, "name and natural_language required")
    if body.get("compiled"):
        compiled, explanation, model_used = body["compiled"], body.get("explanation", ""), body.get("model", "pasted")
    else:
        tables = db.query(models.TableDef).filter_by(group_id=gid).all()
        snap = ", ".join(f"{t.key}" for t in tables)
        compiled, explanation, model_used = ai_mod.ai_compile_rule(nl, snap)
    r = models.Rule(group_id=gid, name=body["name"], natural_language=nl,
                    compiled=compiled, explanation=explanation or body.get("explanation", ""),
                    status="draft", scope=body.get("scope", "general"),
                    scope_ref=body.get("scope_ref"), ai_model=model_used, created_by=me.id)
    db.add(r)
    db.commit()
    return {"id": r.id, "status": r.status, "compiled": r.compiled, "explanation": r.explanation, "model": model_used}


@app.post("/api/rules/{rid}/confirm")
def confirm_rule(rid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.Rule).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "rule not found")
    require_admin(r.group_id, user, db)
    admin = db.query(models.Membership).filter_by(group_id=r.group_id, user_id=user.id).first()
    r.status = "confirmed"
    post_message(db, r.group_id, f"confirmed the rule '{r.name}': {r.explanation}", kind="system",
                 author=admin.display_name if admin else user.name,
                 member_id=admin.id if admin else None)
    db.commit()
    return {"id": r.id, "status": "confirmed"}


@app.post("/api/rules/{rid}/test")
def test_rule(rid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.Rule).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "rule not found")
    me_in(db, r.group_id, user)
    ok, reasons = evaluate(r.compiled, body.get("profile", {}), body.get("submission", {}))
    return {"passed": ok, "reasons": reasons}


# ---------- forms ----------
@app.get("/api/groups/{gid}/forms")
def list_forms(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    out = []
    for f in db.query(models.FormDef).filter_by(group_id=gid).all():
        n = db.query(models.FormSubmission).filter_by(form_id=f.id).count()
        mine = db.query(models.FormSubmission).filter_by(form_id=f.id, member_id=me.id).count()
        out.append({"id": f.id, "title": f.title, "description": f.description,
                    "fields": f.fields or [], "table_id": f.table_id, "rule_id": f.rule_id,
                    "status": f.status, "responses": n,
                    "my_responses": mine, "allow_multiple": bool(f.allow_multiple)})
    return out


@app.post("/api/groups/{gid}/forms")
def create_form(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member = me_in(db, gid, user)
    if not (body.get("title") or "").strip():
        raise HTTPException(400, "title required")
    f = models.FormDef(group_id=gid, table_id=body.get("table_id"), title=body["title"].strip(),
                       description=body.get("description", ""), fields=body.get("fields", []),
                       rule_id=body.get("rule_id"), status=body.get("status", "published"),
                       allow_multiple=bool(body.get("allow_multiple", False)))
    db.add(f)
    db.flush()
    # shared into the chat as a fillable card, like the template's form cards
    post_message(db, gid, f"shared the form '{f.title}'.", kind="form", author=member.display_name,
                 member_id=member.id, payload={"form_id": f.id, "title": f.title,
                                               "blurb": f.description or f"{len(f.fields or [])} questions"})
    db.commit()
    return {"id": f.id, "title": f.title}


@app.post("/api/forms/{fid}/submit")
def submit_form(fid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.FormDef).filter_by(id=fid).first()
    if not f:
        raise HTTPException(404, "form not found")
    member = me_in(db, f.group_id, user)
    data = body.get("data", {})
    if not f.allow_multiple:
        prior = db.query(models.FormSubmission).filter_by(form_id=fid, member_id=member.id, status="accepted").count()
        if prior:
            raise HTTPException(400, "you already submitted this form — one response per member")
    missing = [fld["name"] for fld in (f.fields or []) if fld.get("required") and not data.get(fld["name"])]
    if missing:
        raise HTTPException(400, f"Missing required: {', '.join(missing)}")
    # Only the form's attached rule enforces (compile-once per surface).
    # Cross-field rules (e.g. stake coverage) need both sides present, which
    # only exists at task-accept time — listing forms stay open to senders.
    rules = []
    if f.rule_id:
        r = db.query(models.Rule).filter_by(id=f.rule_id).first()
        if r and r.status == "confirmed":
            rules.append(r)
    prof = member.profile or {}
    for r in rules:
        ok, reasons = evaluate(r.compiled, prof, data)
        if not ok:
            sub = models.FormSubmission(form_id=fid, group_id=f.group_id, member_id=member.id,
                                       data=data, status="rejected", reason="; ".join(reasons))
            db.add(sub)
            db.commit()
            raise HTTPException(400, f"Blocked by '{r.name}': " + "; ".join(reasons))
    sub = models.FormSubmission(form_id=fid, group_id=f.group_id, member_id=member.id, data=data, status="accepted")
    db.add(sub)
    if f.table_id:
        db.add(models.Record(table_id=f.table_id, group_id=f.group_id, member_ref_id=member.id,
                             data={"_form": f.title, **data}, created_by_member_id=member.id))
    patch = {k: v for k, v in data.items() if v not in (None, "") and k in
             ("city", "fleet", "availability", "rating", "verification", "transport_mode", "skills", "stake_balance", "full_name", "phone")}
    if patch:
        member.profile = {**prof, **patch}
    post_message(db, f.group_id, f"{member.display_name} submitted '{f.title}'.", kind="form", author=member.display_name,
                 member_id=member.id, payload={"form_id": fid})
    push_inbox(db, f.group_id, f"Form accepted: {f.title}", f"{member.display_name}'s submission passed validation.",
               kind="form", member_id=member.id, ref_type="form", ref_id=fid)
    fire_event(db, f.group_id, "form.submitted", {"trigger_ref": fid, "member_id": member.id, "note": f.title})
    db.commit()
    return {"submission_id": sub.id, "status": "accepted"}


# ---------- tasks ----------
@app.get("/api/groups/{gid}/tasks")
def list_tasks(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    out = []
    for t in db.query(models.TaskDef).filter_by(group_id=gid).order_by(models.TaskDef.id).all():
        offers = db.query(models.TaskOffer).filter_by(task_id=t.id).all()
        mine = next((o for o in offers if o.member_id == me.id), None)
        out.append({"id": t.id, "title": t.title, "description": t.description,
                    "payout_text": t.payout_text, "rule_id": t.rule_id, "audience": t.audience or {},
                    "status": t.status, "offered": len(offers),
                    "responded": len([o for o in offers if o.status != "offered"]),
                    "my_offer": {"id": mine.id, "status": mine.status} if mine else None})
    return out


@app.post("/api/groups/{gid}/tasks")
def create_task(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    if not (body.get("title") or "").strip():
        raise HTTPException(400, "title required")
    t = models.TaskDef(group_id=gid, title=body["title"].strip(), description=body.get("description", ""),
                       payout_text=body.get("payout_text", ""), rule_id=body.get("rule_id"),
                       audience=body.get("audience", {}), write_table_id=body.get("write_table_id"),
                       write_template=body.get("write_template", {}),
                       status=body.get("status", "live"), created_by=me.id)
    db.add(t)
    db.flush()
    post_message(db, gid, f"posted the task '{t.title}'.", kind="task", author=me.display_name,
                 member_id=me.id, payload={"task_id": t.id})
    n = create_offers_for_task(db, t, announce=False) if t.status == "live" else 0
    db.commit()
    return {"id": t.id, "title": t.title, "offered": n}


@app.post("/api/groups/{gid}/tasks/{tid}/share")
def share_task(gid: int, tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Post an existing task (made anywhere) into chat as a tappable card."""
    member = me_in(db, gid, user)
    t = db.query(models.TaskDef).filter_by(id=tid, group_id=gid).first()
    if not t:
        raise HTTPException(404, "task not found in this group")
    post_message(db, gid, f"shared the task '{t.title}'.", kind="task", author=member.display_name,
                 member_id=member.id, payload={"task_id": t.id})
    db.commit()
    return {"shared": tid}


@app.post("/api/groups/{gid}/forms/{fid}/share")
def share_form(gid: int, fid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Post an existing form into chat as a fillable card."""
    member = me_in(db, gid, user)
    f = db.query(models.FormDef).filter_by(id=fid, group_id=gid).first()
    if not f:
        raise HTTPException(404, "form not found in this group")
    post_message(db, gid, f"shared the form '{f.title}'.", kind="form", author=member.display_name,
                 member_id=member.id, payload={"form_id": f.id, "title": f.title,
                                               "blurb": f.description or f"{len(f.fields or [])} questions"})
    db.commit()
    return {"shared": fid}


@app.get("/api/tasks/{tid}/offers")
def task_offers(tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TaskDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "task not found")
    me_in(db, t.group_id, user)
    out = []
    for o in db.query(models.TaskOffer).filter_by(task_id=tid).all():
        m = db.query(models.Membership).filter_by(id=o.member_id).first()
        out.append({"id": o.id, "member_id": o.member_id, "member": m.display_name if m else "?",
                    "status": o.status, "response_text": o.response_text})
    return out


@app.post("/api/tasks/{tid}/offer")
def offer_task(tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TaskDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "task not found")
    me = me_in(db, t.group_id, user)
    from app.workflows import member_name as _mn
    n = create_offers_for_task(db, t, author=_mn(db, t.group_id, t.created_by) if t.created_by else me.display_name)
    db.commit()
    return {"offered": n}


@app.post("/api/offers/{oid}/respond")
def respond_offer(oid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.query(models.TaskOffer).filter_by(id=oid).first()
    if not o:
        raise HTTPException(404, "offer not found")
    t = db.query(models.TaskDef).filter_by(id=o.task_id).first()
    me = me_in(db, o.group_id, user)
    if o.member_id != me.id and me.role != "admin":
        raise HTTPException(403, "this offer isn't yours")
    m = db.query(models.Membership).filter_by(id=o.member_id).first()
    decision = (body.get("decision") or "accept").lower()
    want = "completed" if body.get("complete") else ("accepted" if decision in ("accept", "accepted") else "declined")
    if o.status == want or (want == "accepted" and o.status == "completed"):
        db.commit()  # idempotent: no duplicate inboxes, messages or events
        return {"id": o.id, "status": o.status, "already": True}
    if decision in ("accept", "accepted"):
        if t and t.rule_id:
            r = db.query(models.Rule).filter_by(id=t.rule_id).first()
            if r and r.status == "confirmed":
                submission = {"value": (t.write_template or {}).get("value", body.get("value"))}
                ok, reasons = evaluate(r.compiled, (m.profile or {}) if m else {}, submission)
                if not ok:
                    raise HTTPException(400, f"Not eligible — {r.name}: " + "; ".join(reasons))
        o.status = "accepted"
        o.response_text = body.get("response_text", "")
        o.responded_at = utcnow().replace(tzinfo=None)
        if m and t:
            post_message(db, t.group_id, f"{m.display_name} accepted '{t.title}'.", kind="task",
                         author=m.display_name, member_id=m.id)
            push_inbox(db, t.group_id, f"Accepted: {t.title}", "Your acceptance is registered.", kind="task", member_id=m.id, ref_type="task", ref_id=t.id)
            if t.write_table_id:
                db.add(models.Record(table_id=t.write_table_id, group_id=t.group_id, member_ref_id=m.id,
                                     data={"task": t.title, **(t.write_template or {})}, created_by_member_id=m.id))
            fire_event(db, t.group_id, "offer.accepted", {"trigger_ref": t.id, "member_id": m.id, "note": t.title})
            fire_event(db, t.group_id, "task.accepted", {"trigger_ref": t.id, "member_id": m.id, "note": t.title})
    else:
        o.status = "declined"
        o.response_text = body.get("response_text", "")
        o.responded_at = utcnow().replace(tzinfo=None)
    if body.get("complete"):
        o.status = "completed"
        if t:
            fire_event(db, t.group_id, "task.completed", {"trigger_ref": t.id, "member_id": o.member_id, "note": t.title})
    db.commit()
    return {"id": o.id, "status": o.status}


# ---------- chat ----------
@app.get("/api/groups/{gid}/messages")
def list_messages(gid: int, limit: int = 100, since_id: int = 0,
                  user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    q = db.query(models.Message).filter_by(group_id=gid)
    if since_id:
        q = q.filter(models.Message.id > since_id)
    msgs = q.order_by(models.Message.id).all()
    msgs = msgs[-min(max(limit, 1), 200):]
    return [{"id": m.id, "author": m.author_name, "member_id": m.member_id, "kind": m.kind,
             "body": m.body, "payload": m.payload or {},
             "at": m.created_at.isoformat() if m.created_at else None} for m in msgs]


@app.post("/api/groups/{gid}/messages")
def post_chat(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member = me_in(db, gid, user)
    text = (body.get("body") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    if len(text) > 4000:
        raise HTTPException(400, "message too long (max 4000 chars)")
    author = member.display_name
    if text.startswith("/task "):
        title = text[6:].strip() or "Untitled task"
        t = models.TaskDef(group_id=gid, title=title, description="Created from chat.",
                           audience={}, status="live", created_by=member.id)
        db.add(t)
        db.flush()
        n = create_offers_for_task(db, t, author=member.display_name)
        db.commit()
        return {"slash": "task", "task_id": t.id, "offered": n}
    if text.startswith("/form "):
        title = text[6:].strip() or "Untitled form"
        f = models.FormDef(group_id=gid, title=title, description="Created from chat.", fields=[
            {"name": "answer", "label": "Answer", "type": "text", "required": True}])
        db.add(f)
        db.flush()
        post_message(db, gid, f"shared the form '{title}'.", kind="form",
                     member_id=member.id, payload={"form_id": f.id, "title": title, "blurb": "1 question"})
        db.commit()
        return {"slash": "form", "form_id": f.id}
    if text.startswith("/query "):
        q = analyst_query(db, gid, text[7:])
        m = models.Message(group_id=gid, member_id=member.id,
                           author_name="Platform", kind="query",
                           body=f"Found {q['count']} match(es), avg rating {q['avg_rating']}.",
                           payload=q)
        db.add(m)
        db.commit()
        return {"slash": "query", **q}
    if text.startswith("/agent "):
        agent = db.query(models.AgentDef).filter_by(group_id=gid, active=True).first()
        if not agent:
            raise HTTPException(400, "no active agent")
        run = run_agent(db, agent, input_query=text[7:], target_member_id=member.id)
        db.flush()
        top = (run.result_items or [{}])[0]
        post_message(db, gid, f"agent '{agent.name}' proposed: {top.get('title', 'see Agents tab')}.", kind="agent",
                     member_id=member.id, payload={"agent_run_id": run.id, "agent": agent.name})
        db.commit()
        return {"slash": "agent", "agent_run_id": run.id, "items": run.result_items}
    if text.startswith("/ask "):
        q = (text[6:].strip() or "show me everything")
        tables = db.query(models.TableDef).filter_by(group_id=gid).all()
        from app.ai import ai_ask_plan
        from app.rules import _compare as _cmp
        spec = [{"key": t.key, "columns": [{"name": c.get("name", ""), "type": c.get("type", "text"),
                                            "options": c.get("options", [])} for c in (t.columns or [])]}
                for t in tables]
        plan, model = ai_ask_plan(q, spec)
        table = next((t for t in tables if t.key == (plan.get("table_key") if plan else "")), None) or (tables[0] if tables else None)
        op = (plan.get("op") if plan else "list") or "list"
        col = (plan.get("column") if plan else "") or ""
        filt = (plan.get("filter") if plan else {}) or {}
        rows = []
        if table:
            for r in db.query(models.Record).filter_by(table_id=table.id).all():
                d = r.data or {}
                if all(_cmp(d.get(k), "==", v) for k, v in filt.items()):
                    rows.append(d)
        nums = [float(x[col]) for x in rows if isinstance(x.get(col), (int, float))]
        fname = " · ".join(f"{k}={v}" for k, v in filt.items())
        if op == "count":
            answer = f"{len(rows)} record(s)" + (f" where {fname}" if fname else "") + "."
        elif op in ("sum", "avg", "min", "max") and nums:
            val = {"sum": sum(nums), "avg": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}[op]
            word = {"sum": "Total", "avg": "Average", "min": "Lowest", "max": "Highest"}[op]
            val_s = f"{val:,.2f}".rstrip("0").rstrip(".")
            answer = f"{word} {col} across {len(rows)} record(s): {val_s}" + (f" (where {fname})" if fname else "") + "."
        else:
            op = "list" if op not in ("count",) else op
            answer = f"{len(rows)} matching record(s)" + (f" where {fname}" if fname else "") + "."
        sample = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:5]]
        post_message(db, gid, text, kind="chat", author=author, member_id=member.id)
        post_message(db, gid, answer, kind="query", author="Platform",
                     payload={"answer": answer, "rows": sample, "op": op,
                              "table": table.key if table else "", "model": model})
        db.commit()
        return {"slash": "ask", "answer": answer}
    if text.startswith("/schedule "):
        s = models.Schedule(group_id=gid, name=text[10:60], description="Created from chat.",
                            kind="daily", time_of_day="09:00", action="report",
                            action_config={"note": text}, created_by=member.id)
        normalize_schedule(s)
        db.add(s)
        db.commit()
        return {"slash": "schedule", "schedule_id": s.id}
    m = models.Message(group_id=gid, member_id=member.id, author_name=author, kind="chat", body=text, payload={})
    if body.get("reply_to"):
        orig = db.query(models.Message).filter_by(id=body["reply_to"], group_id=gid).first()
        if orig:
            m.payload = {"reply_to": {"id": orig.id, "author": orig.author_name,
                                      "body": (orig.body or "")[:140]}}
    if body.get("kind") == "file" and isinstance(body.get("file"), dict):
        f = body["file"]
        if not f.get("url", "").startswith("/static/uploads/"):
            raise HTTPException(400, "bad file reference")
        m.kind = "file"
        m.body = (f.get("filename") or "attachment")[:160]
        m.payload = {**m.payload, "url": f["url"], "filename": f.get("filename", "")[:160],
                     "mime": f.get("mime", ""), "size": f.get("size", 0)}
    db.add(m)
    try:
        tables = db.query(models.TableDef).filter_by(group_id=gid).all()
        snap = ", ".join(t.key for t in tables)
        hint = ai_mod.ai_chat_structured_suggest(text, snap)
        if hint and hint.get("table_key"):
            m.payload = {"suggested_table": hint.get("table_key"), "why": hint.get("why", "")}
    except Exception:
        pass
    db.commit()
    return {"id": m.id, "kind": m.kind}


@app.post("/api/groups/{gid}/typing")
def typing(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member = me_in(db, gid, user)
    _typing[(gid, member.id)] = {"name": member.display_name, "until": time.time() + 4}
    return {"ok": True}


@app.get("/api/groups/{gid}/typing")
def typing_who(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    now = time.time()
    return [v["name"] for (g, mid), v in _typing.items()
            if g == gid and v["until"] > now and mid != me.id]


ALLOWED_REACTIONS = {"✅", "👀", "⏳", "🙏", "⚠️", "❤️"}


@app.get("/api/groups/{gid}/reactions")
def list_reactions(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    out = []
    for r in db.query(models.MessageReaction).filter_by(group_id=gid).order_by(models.MessageReaction.id).limit(2000).all():
        m = db.query(models.Membership).filter_by(id=r.member_id).first()
        out.append({"message_id": r.message_id, "member_id": r.member_id,
                    "member": m.display_name if m else "?", "emoji": r.emoji})
    return out


@app.post("/api/messages/{mid}/reactions")
def toggle_reaction(mid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    src = db.query(models.Message).filter_by(id=mid).first()
    if not src:
        raise HTTPException(404, "message not found")
    member = me_in(db, src.group_id, user)
    emoji = body.get("emoji", "")
    if emoji not in ALLOWED_REACTIONS:
        raise HTTPException(400, f"reactions are limited to {' '.join(sorted(ALLOWED_REACTIONS))}")
    existing = db.query(models.MessageReaction).filter_by(
        message_id=mid, member_id=member.id, emoji=emoji).first()
    if existing:
        db.delete(existing)
        db.commit()
        return {"toggled": "removed", "emoji": emoji}
    db.add(models.MessageReaction(message_id=mid, group_id=src.group_id, member_id=member.id, emoji=emoji))
    db.commit()
    return {"toggled": "added", "emoji": emoji}


@app.post("/api/groups/{gid}/ask")
def ask_records(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Chat with records: NL question -> answered with real aggregations."""
    from app.ai import ai_ask_plan
    from app.rules import _compare
    member = me_in(db, gid, user)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")
    tables = db.query(models.TableDef).filter_by(group_id=gid).all()
    if not tables:
        raise HTTPException(400, "no tables in this group yet")
    spec = [{"key": t.key, "columns": [{"name": c.get("name", ""), "type": c.get("type", "text"),
                                        "options": c.get("options", [])} for c in (t.columns or [])]}
            for t in tables]
    plan, model = ai_ask_plan(question, spec)
    table = next((t for t in tables if t.key == (plan.get("table_key") if plan else "")), None) or tables[0]
    op = (plan.get("op") if plan else "list") or "list"
    col = (plan.get("column") if plan else "") or ""
    filt = (plan.get("filter") if plan else {}) or {}
    rows = []
    for r in db.query(models.Record).filter_by(table_id=table.id).all():
        d = r.data or {}
        if all(_compare(d.get(k), "==", v) for k, v in filt.items()):
            rows.append(d)
    nums = [float(x[col]) for x in rows if isinstance(x.get(col), (int, float))]
    fname = " · ".join(f"{k}={v}" for k, v in filt.items())
    if op == "count":
        answer = f"{len(rows)} record(s) in {table.name}" + (f" where {fname}" if fname else "") + "."
    elif op in ("sum", "avg", "min", "max") and nums:
        val = {"sum": sum(nums), "avg": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}[op]
        word = {"sum": "Total", "avg": "Average", "min": "Lowest", "max": "Highest"}[op]
        val_s = f"{val:,.2f}".rstrip("0").rstrip(".")
        answer = f"{word} {col} across {len(rows)} record(s): {val_s}" + (f" (where {fname})" if fname else "") + "."
    elif op in ("sum", "avg", "min", "max"):
        answer = f"No numbers in {col or 'that column'} to compute — showing {len(rows)} matching record(s)."
        op = "list"
    else:
        answer = f"{len(rows)} matching record(s) in {table.name}" + (f" where {fname}" if fname else "") + "."
    sample = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:5]]
    post_message(db, gid, question, kind="chat", author=member.display_name, member_id=member.id)
    post_message(db, gid, answer, kind="query", author="Platform",
                 payload={"answer": answer, "rows": sample, "op": op, "table": table.key, "model": model})
    db.commit()
    return {"answer": answer, "rows": sample, "op": op, "model": model}


@app.post("/api/messages/{mid}/forward")
def forward_message(mid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    src = db.query(models.Message).filter_by(id=mid).first()
    if not src:
        raise HTTPException(404, "message not found")
    me_in(db, src.group_id, user)
    to_gid = body.get("to_group_id")
    target = me_in(db, to_gid, user) if to_gid else None
    if not target:
        raise HTTPException(400, "pick one of your groups to forward to")
    if to_gid == src.group_id:
        raise HTTPException(400, "already in this group — forwarding is for other groups")
    payload = dict(src.payload or {})
    payload["forwarded_from"] = {"group_id": src.group_id, "author": src.author_name}
    fwd = models.Message(group_id=to_gid, member_id=target.id, author_name=target.display_name,
                         kind=src.kind if src.kind in ("chat", "task", "form") else "chat",
                         body=src.body, payload=payload)
    db.add(fwd)
    db.commit()
    return {"id": fwd.id, "to_group_id": to_gid}


# ---------- inbox ----------
@app.get("/api/groups/{gid}/inbox")
def get_inbox(gid: int, scope: str = "all",
              user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member = me_in(db, gid, user)
    member_id = member.id
    q = db.query(models.InboxItem).filter_by(group_id=gid).order_by(models.InboxItem.id.desc()).limit(100).all()
    out = []
    for i in q:
        if scope == "personal" and (i.member_id != member_id):
            continue
        if scope == "public" and i.member_id is not None:
            continue
        if scope == "mine" and not (i.member_id in (None, member_id)):
            continue
        out.append({"id": i.id, "title": i.title, "body": i.body, "kind": i.kind,
                    "member_id": i.member_id, "scope": "public" if i.member_id is None else "personal",
                    "ref_type": i.ref_type, "ref_id": i.ref_id, "is_read": i.is_read,
                    "at": i.created_at.isoformat() if i.created_at else None})
    return out


@app.post("/api/groups/{gid}/inbox")
def push_inbox_api(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = me_in(db, gid, user)
    if me.role != "admin" and body.get("member_id") not in (None, me.id):
        raise HTTPException(403, "you can only send inbox items to yourself — admins can target anyone")
    if body.get("member_id") is None and me.role != "admin":
        raise HTTPException(403, "only admins can broadcast public items")
    item = push_inbox(db, gid, body.get("title", "Update"), body.get("body", ""),
                      kind=body.get("kind", "info"), member_id=body.get("member_id"),
                      ref_type=body.get("ref_type", ""), ref_id=body.get("ref_id"))
    db.commit()
    return {"id": item.id, "scope": "public" if item.member_id is None else "personal"}


@app.post("/api/inbox/{iid}/read")
def mark_read(iid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    i = db.query(models.InboxItem).filter_by(id=iid).first()
    if not i:
        raise HTTPException(404, "not found")
    me_in(db, i.group_id, user)
    i.is_read = True
    db.commit()
    return {"id": i.id, "is_read": True}


# ---------- views / pages (shared dashboards, visible to every member) ----------
@app.get("/api/groups/{gid}/views")
def list_views(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    out = []
    for v in db.query(models.PageView).filter_by(group_id=gid).all():
        rows = compute_view_rows(db, v)
        nums = [x for r in rows for x in r.values() if isinstance(x, (int, float))]
        out.append({"id": v.id, "title": v.title, "description": v.description,
                    "source_table_id": v.source_table_id, "columns": v.columns or [],
                    "filter": v.filter or {}, "template": v.template,
                    "stat": {"rows": len(rows),
                             "avg": round(sum(nums) / len(nums), 2) if nums else None}})
    return out


@app.post("/api/groups/{gid}/views")
def create_view(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    v = models.PageView(group_id=gid, title=(body.get("title") or "Untitled view").strip(),
                        description=body.get("description", ""),
                        source_table_id=body.get("source_table_id"),
                        columns=body.get("columns", []), filter=body.get("filter", {}),
                        template=body.get("template", "table"))
    db.add(v)
    db.commit()
    return {"id": v.id, "title": v.title}


@app.post("/api/groups/{gid}/views/generate")
def generate_view(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    prompt = body.get("prompt", "")
    tables = db.query(models.TableDef).filter_by(group_id=gid).all()
    if not tables:
        raise HTTPException(400, "no tables")
    snap = "; ".join(f"{t.key}: {[c.get('name') for c in (t.columns or [])]}" for t in tables)
    planned, model_used = ai_mod.ai_plan_view(prompt, snap)
    table = None
    if planned and planned.get("table_key"):
        table = db.query(models.TableDef).filter_by(group_id=gid, key=planned["table_key"]).first()
    table = table or tables[0]
    cols = (planned or {}).get("columns") or [c.get("name") for c in (table.columns or [])][:5]
    filt = (planned or {}).get("filter") if isinstance((planned or {}).get("filter"), dict) else {}
    template = (planned or {}).get("template", "table")
    summary = (planned or {}).get("summary", "")
    v = models.PageView(group_id=gid, title=body.get("title") or f"View: {prompt[:40]}",
                        description=summary or f"Generated from '{prompt}' ({model_used})",
                        source_table_id=table.id, columns=cols, filter=filt,
                        template=template)
    db.add(v)
    db.commit()
    return {"id": v.id, "table_key": table.key, "columns": cols, "filter": filt,
            "template": template, "summary": summary, "model": model_used}


@app.post("/api/views/{vid}/insight")
def view_insight(vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """AI reads the dashboard and says what stands out + what to do next."""
    from app.ai import ai_view_insight
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    me_in(db, v.group_id, user)
    rows = compute_view_rows(db, v)
    insight, model = ai_view_insight(v.title, v.columns or [], rows)
    return {"insight": insight, "model": model, "rows": len(rows)}


@app.get("/api/views/{vid}/rows")
def view_rows(vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    me_in(db, v.group_id, user)
    return {"view": {"id": v.id, "title": v.title, "template": v.template}, "rows": compute_view_rows(db, v)}


def render_snapshot_page(group_name, view, rows):
    """GenUI: one frozen snapshot rendered as a standalone visual page."""
    import html as _h
    esc = lambda x: _h.escape(str(x if x is not None else "—"))
    cols = [c for c in (view.columns or []) if not c.startswith("_")]
    data_rows = [{c: r.get(c) for c in cols} for r in rows]
    nums = [x for r in data_rows for x in r.values() if isinstance(x, (int, float))]
    # first numeric column drives the bar visualization
    bar_col = next((c for c in cols if any(isinstance(r.get(c), (int, float)) for r in data_rows)), None)
    bars = ""
    if bar_col:
        top = sorted([r for r in data_rows if isinstance(r.get(bar_col), (int, float))],
                     key=lambda r: -r[bar_col])[:8]
        mx = max([r[bar_col] for r in top]) or 1
        label = cols[0] if cols else bar_col
        for r in top:
            w = max(4, int(100 * r[bar_col] / mx))
            bars += (f'<div class="brow"><span class="blab">{esc(r.get(label))}</span>'
                     f'<div class="btrack"><div class="bfill" style="width:{w}%"></div></div>'
                     f'<span class="bval">{esc(r[bar_col])}</span></div>')
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(r.get(c))}</td>" for c in cols) + "</tr>" for r in data_rows)
    stamp = utcnow().strftime("%d %b %Y, %H:%M UTC")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(view.title)} — {esc(group_name)} · Platform</title>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#f7f6f3;color:#37352f;margin:0;padding:32px 16px}}
.wrap{{max-width:860px;margin:auto;background:#fff;border:1px solid #e9e8e4;border-radius:12px;padding:28px;box-shadow:0 4px 12px rgba(0,0,0,.06)}}
.kicker{{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#9b9a97;font-weight:700}}
h1{{margin:4px 0 2px;font-size:26px}} .sub{{color:#6f6e69;font-size:13px;margin-bottom:18px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}}
.card{{background:#f1f0ec;border-radius:8px;padding:12px}} .card b{{font-size:22px;display:block}} .card span{{font-size:11px;color:#6f6e69}}
h2{{font-size:14px;margin:20px 0 10px}} .brow{{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:12px}}
.blab{{width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}} .btrack{{flex:1;background:#f1f0ec;border-radius:99px;height:10px}}
.bfill{{background:#2383e2;height:10px;border-radius:99px}} .bval{{width:70px;text-align:right;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}} th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #e9e8e4}}
th{{color:#9b9a97;text-transform:uppercase;font-size:10px;letter-spacing:.05em}}
.foot{{margin-top:20px;font-size:11px;color:#9b9a97}}
</style></head><body><div class="wrap">
<div class="kicker">Platform · {esc(group_name)} · frozen snapshot</div>
<h1>{esc(view.title)}</h1><div class="sub">{esc(view.description or '')} — snapshot {stamp}, {len(data_rows)} rows.</div>
<div class="cards"><div class="card"><b>{len(data_rows)}</b><span>rows</span></div>
<div class="card"><b>{(round(sum(nums)/len(nums),2)) if nums else '—'}</b><span>avg value</span></div>
<div class="card"><b>{(max(nums)) if nums else '—'}</b><span>peak value</span></div></div>
{f'<h2>Top by {esc(bar_col)}</h2>{bars}' if bars else ''}
<h2>All rows</h2><table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td>No rows in this snapshot.</td></tr>'}</tbody></table>
<div class="foot">Generated by Platform from live group data. Re-publish the view for a fresh snapshot.</div>
</div></body></html>"""


@app.post("/api/views/{vid}/publish")
def publish_view(vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Freeze this view into a static page with its own external link."""
    import secrets as _s
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    me_in(db, v.group_id, user)
    g = db.query(models.Group).filter_by(id=v.group_id).first()
    rows = compute_view_rows(db, v)
    snap = models.ViewSnapshot(view_id=v.id, group_id=v.group_id, token=_s.token_urlsafe(12),
                               title=v.title, html=render_snapshot_page(g.name if g else "", v, rows),
                               row_count=len(rows))
    db.add(snap)
    db.commit()
    return {"token": snap.token, "url": f"/p/{snap.token}", "rows": len(rows)}


@app.get("/api/views/{vid}/snapshots")
def list_snapshots(vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    me_in(db, v.group_id, user)
    return [{"token": s.token, "url": f"/p/{s.token}", "rows": s.row_count,
             "at": s.created_at.isoformat() if s.created_at else None}
            for s in db.query(models.ViewSnapshot).filter_by(view_id=vid).order_by(models.ViewSnapshot.id.desc()).limit(10).all()]


@app.get("/p/{token}", response_class=HTMLResponse)
def public_page(token: str, db: Session = Depends(get_db)):
    """External link: anyone with the URL sees the frozen page. No login."""
    s = db.query(models.ViewSnapshot).filter_by(token=token).first()
    if not s:
        raise HTTPException(404, "page not found — ask the group for a fresh link")
    return HTMLResponse(s.html)


@app.post("/api/groups/{gid}/views/{vid}/share")
def share_view(gid: int, vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Share a portal to a view: drops an interactive dashboard card into chat."""
    member = me_in(db, gid, user)
    v = db.query(models.PageView).filter_by(id=vid, group_id=gid).first()
    if not v:
        raise HTTPException(404, "view not found")
    post_message(db, gid, f"shared the '{v.title}' dashboard.", kind="view", author=member.display_name,
                 member_id=member.id, payload={"view_id": v.id, "title": v.title,
                                               "blurb": v.description or "Live from group tables"})
    db.commit()
    return {"shared": vid}


AI_KINDS = ("task", "form", "schedule", "redirect", "table")
AI_MEMBER_KINDS = ("task", "form")  # any member; the rest are admin-only


def _clean_str(v, maxlen):
    v = str(v or "").strip()
    return v[:maxlen] if v else ""


@app.post("/api/groups/{gid}/ai/propose")
def ai_propose_endpoint(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Step 1: turn a prompt into a structured creation spec. Writes nothing."""
    from app.ai import ai_propose
    me_in(db, gid, user)
    kind = (body.get("kind") or "").strip()
    if kind not in AI_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(AI_KINDS)}")
    tables = db.query(models.TableDef).filter_by(group_id=gid).all()
    rules = db.query(models.Rule).filter_by(group_id=gid, status="confirmed").all()
    snap = ("tables: " + ", ".join(f"{t.key}({','.join(c.get('name', '') for c in (t.columns or []))})" for t in tables)
            + "; rules: " + ", ".join(r.name for r in rules))
    spec, model, warnings = ai_propose(kind, body.get("prompt", ""), snap,
                                       {"tables": [t.key for t in tables], "rules": [r.name for r in rules]})
    if not spec:
        raise HTTPException(400, (warnings or ["could not understand that — try being more specific"])[0])
    return {"kind": kind, "spec": spec, "model": model, "warnings": warnings}


@app.post("/api/groups/{gid}/ai/apply")
def ai_apply_endpoint(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Step 2: validate the spec strictly, then create exactly what it says."""
    kind = (body.get("kind") or "").strip()
    spec = body.get("spec") or {}
    if kind not in AI_KINDS or not isinstance(spec, dict):
        raise HTTPException(400, "kind and spec required")
    if kind in AI_MEMBER_KINDS:
        me = me_in(db, gid, user)
    else:
        me = require_admin(gid, user, db)
    title = _clean_str(spec.get("title") or spec.get("name"), 160)
    if kind == "task":
        if not title:
            raise HTTPException(400, "spec needs a title")
        tags = [str(x)[:32] for x in (spec.get("audience_tags") or []) if str(x).strip()][:5]
        rule_id = None
        rn = _clean_str(spec.get("rule_name"), 160)
        if rn:
            hit = next((r for r in db.query(models.Rule).filter_by(group_id=gid, status="confirmed").all()
                        if r.name.lower() == rn.lower() or rn.lower() in r.name.lower()), None)
            if hit:
                rule_id = hit.id
        t = models.TaskDef(group_id=gid, title=title, description=_clean_str(spec.get("description"), 500),
                           payout_text=_clean_str(spec.get("payout_text"), 60), rule_id=rule_id,
                           audience={"tags": tags} if tags else {},
                           status="live", created_by=me.id)
        db.add(t)
        db.flush()
        post_message(db, gid, f"posted the task '{title}' (from AI draft).", kind="task",
                     author=me.display_name, member_id=me.id, payload={"task_id": t.id})
        n = create_offers_for_task(db, t, announce=False)
        db.commit()
        return {"id": t.id, "offered": n}
    if kind == "form":
        if not title:
            raise HTTPException(400, "spec needs a title")
        fields = []
        for f in (spec.get("fields") or [])[:8]:
            if not isinstance(f, dict):
                continue
            name = _clean_str(f.get("name"), 40) or "field"
            ftype = f.get("type") if f.get("type") in ("text", "number", "select") else "text"
            fields.append({"name": name, "label": _clean_str(f.get("label") or name, 60),
                           "type": ftype, "required": bool(f.get("required", True)),
                           "options": [str(o)[:40] for o in (f.get("options") or [])[:8]] if ftype == "select" else []})
        if not fields:
            raise HTTPException(400, "spec needs at least one field")
        table_id = spec.get("table_id")
        if table_id and not db.query(models.TableDef).filter_by(id=table_id, group_id=gid).first():
            table_id = None
        if not table_id and spec.get("table_key"):
            hit = db.query(models.TableDef).filter_by(group_id=gid, key=str(spec["table_key"])[:64]).first()
            if hit:
                table_id = hit.id
        rule_id = spec.get("rule_id")
        if rule_id and not db.query(models.Rule).filter_by(id=rule_id, group_id=gid).first():
            rule_id = None
        f = models.FormDef(group_id=gid, table_id=table_id, title=title,
                           description=_clean_str(spec.get("description"), 300), fields=fields,
                           rule_id=rule_id, status="published",
                           allow_multiple=bool(spec.get("allow_multiple", False)))
        db.add(f)
        db.flush()
        post_message(db, gid, f"shared the form '{title}' (from AI draft).", kind="form", author=me.display_name,
                     member_id=me.id, payload={"form_id": f.id, "title": title, "blurb": f"{len(fields)} questions"})
        db.commit()
        return {"id": f.id}
    if kind == "schedule":
        action = spec.get("action")
        if action not in ("inspect", "match", "follow_up", "report", "motivate", "push_inbox", "create_task", "run_agent"):
            raise HTTPException(400, "spec has an unknown schedule action")
        skind = spec.get("kind") if spec.get("kind") in ("once", "interval", "daily", "weekly", "cron") else "daily"
        s = models.Schedule(group_id=gid, name=title or "AI schedule", description=_clean_str(spec.get("note"), 200),
                            kind=skind, time_of_day=_clean_str(spec.get("time_of_day"), 5) or "09:00",
                            interval_seconds=int(spec.get("interval_seconds") or 3600) if skind == "interval" else None,
                            cron_expr=_clean_str(spec.get("cron_expr"), 32),
                            action=action, action_config={"note": _clean_str(spec.get("note"), 200)},
                            active=True, created_by=me.id)
        normalize_schedule(s)
        db.add(s)
        db.commit()
        post_message(db, gid, f"scheduled '{s.name}' ({s.kind} → {s.action}) (from AI draft).", kind="schedule",
                     author=me.display_name, member_id=me.id)
        db.commit()
        return {"id": s.id}
    if kind == "redirect":
        trig = spec.get("trigger_event")
        if trig not in ("offer.accepted", "task.accepted", "task.completed", "form.submitted", "status.confirmed", "schedule.fired", "member.joined"):
            raise HTTPException(400, "spec has an unknown trigger")
        act = spec.get("action")
        if act not in ("push_inbox", "push_public", "create_task", "write_record", "run_agent"):
            raise HTTPException(400, "spec has an unknown action")
        r = models.Redirect(group_id=gid, name=title or "AI flow", trigger_event=trig,
                            action=act, action_config={"title": title, "body": _clean_str(spec.get("message"), 300)},
                            active=True)
        db.add(r)
        db.commit()
        return {"id": r.id}
    # table
    if db.query(models.TableDef).filter_by(group_id=gid).count() >= 15:
        raise HTTPException(400, "Fifteen-table cap reached.")
    if not title:
        raise HTTPException(400, "spec needs a name")
    cols = []
    for c in (spec.get("columns") or [])[:8]:
        if not isinstance(c, dict):
            continue
        cols.append({"name": _clean_str(c.get("name"), 40) or "col",
                     "label": _clean_str(c.get("label") or c.get("name"), 40),
                     "type": c.get("type") if c.get("type") in ("text", "number", "select") else "text"})
    if not cols:
        raise HTTPException(400, "spec needs at least one column")
    key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:64] or "table"
    if db.query(models.TableDef).filter_by(group_id=gid, key=key).first():
        raise HTTPException(400, "a table with that name already exists")
    t = models.TableDef(group_id=gid, key=key, name=title,
                        description=_clean_str(spec.get("description"), 200), columns=cols)
    db.add(t)
    db.commit()
    return {"id": t.id, "key": key}


# ---------- redirects / workflows ----------
@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TaskDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "task not found")
    require_admin(t.group_id, user, db)
    db.query(models.TaskOffer).filter_by(task_id=tid).delete()
    db.delete(t)
    db.commit()
    return {"ok": True}


@app.patch("/api/tasks/{tid}")
def patch_task(tid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    t = db.query(models.TaskDef).filter_by(id=tid).first()
    if not t:
        raise HTTPException(404, "task not found")
    require_admin(t.group_id, user, db)
    for k in ("title", "description", "payout_text"):
        if body.get(k) is not None:
            setattr(t, k, str(body[k])[:500 if k != "title" else 200])
    if body.get("status") in ("live", "draft", "closed"):
        t.status = body["status"]
    if isinstance(body.get("audience"), dict):
        t.audience = body["audience"]
    db.commit()
    return {"id": t.id, "title": t.title, "status": t.status}


@app.delete("/api/forms/{fid}")
def delete_form(fid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.FormDef).filter_by(id=fid).first()
    if not f:
        raise HTTPException(404, "form not found")
    require_admin(f.group_id, user, db)
    db.query(models.FormSubmission).filter_by(form_id=fid).delete()
    db.delete(f)
    db.commit()
    return {"ok": True}


@app.patch("/api/forms/{fid}")
def patch_form(fid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    f = db.query(models.FormDef).filter_by(id=fid).first()
    if not f:
        raise HTTPException(404, "form not found")
    require_admin(f.group_id, user, db)
    if body.get("title"):
        f.title = str(body["title"]).strip()[:160]
    if "description" in body:
        f.description = str(body.get("description") or "")[:500]
    if "allow_multiple" in body:
        f.allow_multiple = bool(body["allow_multiple"])
    if isinstance(body.get("fields"), list) and body["fields"]:
        f.fields = body["fields"][:12]
    db.commit()
    return {"id": f.id, "title": f.title}


@app.delete("/api/views/{vid}")
def delete_view(vid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    require_admin(v.group_id, user, db)
    db.query(models.ViewSnapshot).filter_by(view_id=vid).delete()
    db.delete(v)
    db.commit()
    return {"ok": True}


@app.patch("/api/views/{vid}")
def patch_view(vid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    v = db.query(models.PageView).filter_by(id=vid).first()
    if not v:
        raise HTTPException(404, "view not found")
    require_admin(v.group_id, user, db)
    if body.get("title"):
        v.title = str(body["title"]).strip()[:160]
    if "description" in body:
        v.description = str(body.get("description") or "")[:500]
    if isinstance(body.get("columns"), list):
        v.columns = body["columns"][:12]
    if isinstance(body.get("filter"), dict):
        v.filter = body["filter"]
    db.commit()
    return {"id": v.id, "title": v.title}


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = db.query(models.Schedule).filter_by(id=sid).first()
    if not s:
        raise HTTPException(404, "not found")
    require_admin(s.group_id, user, db)
    db.query(models.ScheduleRun).filter_by(schedule_id=sid).delete()
    db.delete(s)
    db.commit()
    return {"ok": True}


@app.delete("/api/redirects/{rid}")
def delete_redirect(rid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.Redirect).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "not found")
    require_admin(r.group_id, user, db)
    db.delete(r)
    db.commit()
    return {"ok": True}


@app.delete("/api/agents/{aid}")
def delete_agent(aid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.query(models.AgentDef).filter_by(id=aid).first()
    if not a:
        raise HTTPException(404, "not found")
    require_admin(a.group_id, user, db)
    db.delete(a)
    db.commit()
    return {"ok": True}
@app.get("/api/groups/{gid}/redirects")
def list_redirects(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return [{"id": r.id, "name": r.name, "trigger_event": r.trigger_event, "trigger_ref": r.trigger_ref,
             "action": r.action, "action_config": r.action_config or {}, "active": r.active}
            for r in db.query(models.Redirect).filter_by(group_id=gid).all()]


@app.post("/api/groups/{gid}/redirects")
def create_redirect(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(gid, user, db)
    if not body.get("name") or not body.get("trigger_event") or not body.get("action"):
        raise HTTPException(400, "name, trigger_event and action required")
    r = models.Redirect(group_id=gid, name=body["name"], trigger_event=body["trigger_event"],
                        trigger_ref=body.get("trigger_ref"), action=body["action"],
                        action_config=body.get("action_config", {}), active=body.get("active", True))
    db.add(r)
    db.commit()
    return {"id": r.id, "name": r.name}


@app.post("/api/groups/{gid}/redirects/{rid}/toggle")
def toggle_redirect(gid: int, rid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(gid, user, db)
    r = db.query(models.Redirect).filter_by(id=rid, group_id=gid).first()
    if not r:
        raise HTTPException(404, "not found")
    r.active = not r.active
    db.commit()
    return {"id": r.id, "active": r.active}


# ---------- schedules ----------
@app.get("/api/groups/{gid}/schedules")
def list_schedules(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    out = []
    for s in db.query(models.Schedule).filter_by(group_id=gid).order_by(models.Schedule.id).all():
        out.append({"id": s.id, "name": s.name, "description": s.description, "kind": s.kind,
                    "cron_expr": s.cron_expr, "time_of_day": s.time_of_day, "weekday": s.weekday,
                    "interval_seconds": s.interval_seconds, "run_at": s.run_at.isoformat() if s.run_at else None,
                    "action": s.action, "action_config": s.action_config or {}, "active": s.active,
                    "last_run": s.last_run.isoformat() if s.last_run else None,
                    "next_run": s.next_run.isoformat() if s.next_run and str(s.next_run).strip() else None})
    return out


@app.post("/api/groups/{gid}/schedules")
def create_schedule(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me = require_admin(gid, user, db)
    if not body.get("name") or not body.get("action"):
        raise HTTPException(400, "name and action required")
    run_at = None
    if body.get("run_at"):
        try:
            run_at = datetime.fromisoformat(body["run_at"])
        except Exception:
            raise HTTPException(400, "run_at must be ISO datetime")
    s = models.Schedule(group_id=gid, name=body["name"], description=body.get("description", ""),
                        kind=body.get("kind", "interval"),
                        run_at=run_at, interval_seconds=body.get("interval_seconds"),
                        cron_expr=body.get("cron_expr", ""), time_of_day=body.get("time_of_day", ""),
                        weekday=body.get("weekday"), action=body["action"],
                        action_config=body.get("action_config", {}),
                        active=body.get("active", True), created_by=me.id)
    normalize_schedule(s)
    db.add(s)
    db.commit()
    post_message(db, gid, f"scheduled '{s.name}' ({s.kind} → {s.action}).", kind="schedule",
                 author=me.display_name, member_id=me.id)
    db.commit()
    return {"id": s.id, "next_run": s.next_run.isoformat() if s.next_run else None}


@app.post("/api/schedules/{sid}/run-now")
def run_schedule_now(sid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = db.query(models.Schedule).filter_by(id=sid).first()
    if not s:
        raise HTTPException(404, "schedule not found")
    require_admin(s.group_id, user, db)
    status, detail = run_schedule_once(db, s)
    db.commit()
    return {"status": status, "detail": detail}


@app.post("/api/schedules/{sid}/toggle")
def toggle_schedule(sid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = db.query(models.Schedule).filter_by(id=sid).first()
    if not s:
        raise HTTPException(404, "not found")
    require_admin(s.group_id, user, db)
    s.active = not s.active
    from app.scheduler_loop import compute_next
    s.next_run = compute_next(s) if s.active else None
    db.commit()
    return {"id": s.id, "active": s.active}


@app.get("/api/schedules/{sid}/runs")
def schedule_runs(sid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    s = db.query(models.Schedule).filter_by(id=sid).first()
    if not s:
        raise HTTPException(404, "not found")
    me_in(db, s.group_id, user)
    runs = db.query(models.ScheduleRun).filter_by(schedule_id=sid).order_by(models.ScheduleRun.id.desc()).limit(20).all()
    return [{"id": r.id, "status": r.status, "detail": r.detail or {},
             "ran_at": r.ran_at.isoformat() if r.ran_at else None} for r in runs]


# ---------- agents ----------
@app.get("/api/groups/{gid}/agents")
def list_agents(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return [{"id": a.id, "name": a.name, "kind": a.kind, "description": a.description,
             "config": a.config or {}, "active": a.active}
            for a in db.query(models.AgentDef).filter_by(group_id=gid).all()]


@app.post("/api/groups/{gid}/agents")
def create_agent(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(gid, user, db)
    a = models.AgentDef(group_id=gid, name=body.get("name", "New agent"),
                        kind=body.get("kind", "analyst"),
                        description=body.get("description", ""),
                        config=body.get("config", {}))
    db.add(a)
    db.commit()
    return {"id": a.id, "name": a.name, "kind": a.kind}


@app.post("/api/agents/{aid}/run")
def agent_run(aid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    a = db.query(models.AgentDef).filter_by(id=aid).first()
    if not a:
        raise HTTPException(404, "agent not found")
    member = me_in(db, a.group_id, user)
    target = body.get("target_member_id") or member.id
    run = run_agent(db, a, input_query=body.get("query", ""), target_member_id=target)
    db.commit()
    return {"run_id": run.id, "status": run.status, "items": run.result_items,
            "mutation": run.proposed_mutation, "note": run.validation_note}


@app.get("/api/agents/runs")
def agent_runs(group_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, group_id, user)
    runs = db.query(models.AgentRun).filter_by(group_id=group_id).order_by(models.AgentRun.id.desc()).limit(30).all()
    return [{"id": r.id, "agent_id": r.agent_id, "target_member_id": r.target_member_id,
             "query": r.input_query, "items": r.result_items or [],
             "mutation": r.proposed_mutation or {}, "status": r.status,
             "note": r.validation_note} for r in runs]


@app.post("/api/agent-runs/{rid}/confirm")
def agent_confirm(rid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.query(models.AgentRun).filter_by(id=rid).first()
    if not r:
        raise HTTPException(404, "run not found")
    me = me_in(db, r.group_id, user)
    if r.target_member_id and r.target_member_id != me.id and me.role != "admin":
        raise HTTPException(403, "only the targeted member or an admin can confirm this")
    if (body.get("decision") or "confirm") == "reject":
        r.status = "rejected"
        r.decided_at = utcnow().replace(tzinfo=None)
        db.commit()
        return {"id": r.id, "status": "rejected"}
    confirm_agent_run(db, r)
    r.decided_at = utcnow().replace(tzinfo=None)
    db.commit()
    if r.status == "rejected":
        raise HTTPException(400, r.validation_note or "rejected by validation")
    return {"id": r.id, "status": r.status, "note": r.validation_note}


# ---------- in-app browser / external status ----------
@app.get("/api/groups/{gid}/external-events")
def list_external(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return [{"id": e.id, "kind": e.kind, "ref": e.ref_label, "url": e.url, "status": e.status,
             "member_id": e.member_id, "meta": e.meta or {},
             "at": e.created_at.isoformat() if e.created_at else None}
            for e in db.query(models.ExternalEvent).filter_by(group_id=gid).order_by(models.ExternalEvent.id.desc()).limit(50).all()]


@app.post("/api/groups/{gid}/external-events")
def open_external(gid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    member = me_in(db, gid, user)
    e = models.ExternalEvent(group_id=gid, member_id=member.id,
                             kind=body.get("kind", "payment"),
                             ref_label=body.get("ref_label", "External action"),
                             url=body.get("url", ""), status="pending",
                             meta=body.get("meta", {}))
    db.add(e)
    db.flush()
    post_message(db, gid, f"External action opened: {e.kind} — {e.ref_label} (awaiting status…)", kind="status", member_id=member.id)
    db.commit()
    return {"id": e.id, "status": "pending"}


@app.post("/api/external-events/{eid}/report")
def report_external(eid: int, body: dict, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    e = db.query(models.ExternalEvent).filter_by(id=eid).first()
    if not e:
        raise HTTPException(404, "not found")
    me_in(db, e.group_id, user)
    status = (body.get("status") or "confirmed").lower()
    if status not in ("confirmed", "failed", "pending"):
        raise HTTPException(400, "status must be confirmed|failed|pending")
    e.status = status
    e.meta = {**(e.meta or {}), "report_note": body.get("note", ""), "reported_by": "status-tracker"}
    post_message(db, e.group_id, f"{e.kind} {e.ref_label}: {status}.", kind="status")
    if status == "confirmed":
        push_inbox(db, e.group_id, f"{e.kind.title()} confirmed", f"{e.ref_label} — recorded.", kind="status", member_id=e.member_id)
        fire_event(db, e.group_id, "status.confirmed", {"trigger_ref": None, "member_id": e.member_id, "note": e.ref_label})
    db.commit()
    return {"id": e.id, "status": e.status}


# ---------- dashboard ----------
@app.get("/api/groups/{gid}/dashboard")
def dashboard(gid: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    me_in(db, gid, user)
    return {
        "members": db.query(models.Membership).filter_by(group_id=gid).count(),
        "tables": db.query(models.TableDef).filter_by(group_id=gid).count(),
        "records": db.query(models.Record).filter_by(group_id=gid).count(),
        "tasks": db.query(models.TaskDef).filter_by(group_id=gid).count(),
        "open_offers": db.query(models.TaskOffer).filter_by(group_id=gid, status="offered").count(),
        "forms": db.query(models.FormDef).filter_by(group_id=gid).count(),
        "schedules": db.query(models.Schedule).filter_by(group_id=gid, active=True).count(),
        "agents": db.query(models.AgentDef).filter_by(group_id=gid, active=True).count(),
        "pending_status": db.query(models.ExternalEvent).filter_by(group_id=gid, status="pending").count(),
    }


# ---------- frontend (monolith serves UI) ----------
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.post("/api/groups/{gid}/uploads")
def upload_file(gid: int, file: UploadFile = File(...), user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """File uploads for chat: images render inline, other files as cards."""
    import secrets
    me_in(db, gid, user)
    raw = file.filename or "file"
    ext = os.path.splitext(raw)[1][:12].lower() or ".bin"
    safe = secrets.token_hex(8) + ext
    data = file.file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "file too large (max 10 MB)")
    with open(os.path.join(UPLOAD_DIR, safe), "wb") as f:
        f.write(data)
    return {"url": f"/static/uploads/{safe}", "filename": raw[:120],
            "mime": file.content_type or "application/octet-stream", "size": len(data)}


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    index = os.path.join(STATIC_DIR, "app.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse({"app": "platform", "ui": "run with static/app.html present", "api": "/api/health"})


@app.get("/app")
def app_alias():
    return root()
