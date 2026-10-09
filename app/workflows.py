"""Workflow engine: redirects (task chains), inbox routing, generated views."""
from datetime import datetime, timezone
from app import models
from app.rules import evaluate, member_matches_filter


def utcnow():
    return datetime.now(timezone.utc)


def push_inbox(db, group_id, title, body, kind="info", member_id=None, ref_type="", ref_id=None):
    item = models.InboxItem(
        group_id=group_id, member_id=member_id, title=title, body=body,
        kind=kind, ref_type=ref_type, ref_id=ref_id,
    )
    db.add(item)
    return item


def post_message(db, group_id, body, kind="system", author="Platform", member_id=None, payload=None):
    m = models.Message(group_id=group_id, member_id=member_id, author_name=author,
                       kind=kind, body=body, payload=payload or {})
    db.add(m)
    return m


def eligible_members(db, group_id, task):
    """Members eligible for a task: rule checks + audience tags/filter."""
    members = db.query(models.Membership).filter_by(group_id=group_id).all()
    rule = db.query(models.Rule).filter_by(id=task.rule_id).first() if task.rule_id else None
    aud = task.audience or {}
    need_tags = aud.get("tags", []) or []
    filt = aud.get("filter")
    out = []
    for m in members:
        prof = m.profile or {}
        if need_tags and not any(t in (m.tags or []) for t in need_tags):
            continue
        if filt and not member_matches_filter(prof, m.tags or [], filt):
            continue
        if rule and rule.status == "confirmed":
            ok, _ = evaluate(rule.compiled, prof, {})
            if not ok:
                continue
        out.append(m)
    return out


def member_name(db, group_id, member_id):
    if not member_id:
        return "Platform"
    m = db.query(models.Membership).filter_by(id=member_id, group_id=group_id).first()
    return m.display_name if m else "Platform"


def create_offers_for_task(db, task, members=None, announce=True, author="Platform"):
    members = members if members is not None else eligible_members(db, task.group_id, task)
    created = 0
    for m in members:
        exists = db.query(models.TaskOffer).filter_by(task_id=task.id, member_id=m.id).first()
        if exists:
            continue
        db.add(models.TaskOffer(task_id=task.id, group_id=task.group_id, member_id=m.id, status="offered"))
        push_inbox(db, task.group_id, f"Task: {task.title}",
                   f"{task.description} {task.payout_text or ''}".strip()[:300],
                   kind="task", member_id=m.id, ref_type="task", ref_id=task.id)
        created += 1
    if announce:
        post_message(db, task.group_id, f"Task '{task.title}' offered to {len(members)} eligible member(s).",
                     kind="task", author=author, payload={"task_id": task.id, "offered": len(members)})
    return created


def fire_event(db, group_id, event, context=None):
    """Run all active redirects matching (event, optional trigger_ref). Returns actions taken."""
    context = context or {}
    reds = db.query(models.Redirect).filter_by(group_id=group_id, trigger_event=event, active=True).all()
    taken = []
    for r in reds:
        if r.trigger_ref and context.get("trigger_ref") not in (None, r.trigger_ref):
            # if redirect is scoped to a specific task/form, skip others
            if str(context.get("trigger_ref")) != str(r.trigger_ref):
                continue
        cfg = r.action_config or {}
        taken.append(execute_action(db, group_id, r.action, cfg, context, source=f"redirect:{r.name}"))
    if taken:
        db.flush()
    return taken


def execute_action(db, group_id, action, cfg, context=None, source="manual"):
    """Execute one workflow/schedule action. Returns summary dict."""
    context = context or {}
    cfg = cfg or {}
    if action == "push_inbox":
        item = push_inbox(db, group_id, cfg.get("title", "Update"),
                          cfg.get("body", context.get("note", "")),
                          kind=cfg.get("kind", "info"),
                          member_id=cfg.get("member_id") or context.get("member_id"))
        return {"action": action, "inbox_id": item.id if hasattr(item, "id") else None}
    if action == "push_public":
        item = push_inbox(db, group_id, cfg.get("title", "Announcement"),
                          cfg.get("body", ""), kind="info", member_id=None)
        post_message(db, group_id, cfg.get("body", cfg.get("title", "")), kind="system")
        return {"action": action, "inbox_id": None}
    if action == "create_task":
        t = models.TaskDef(
            group_id=group_id, title=cfg.get("title", "Follow-up task"),
            description=cfg.get("description", f"Chained by {source}"),
            payout_text=cfg.get("payout_text", ""),
            rule_id=cfg.get("rule_id"),
            audience=cfg.get("audience", {}),
            status="live", created_by=context.get("member_id"),
        )
        db.add(t)
        db.flush()
        n = create_offers_for_task(db, t, author=member_name(db, group_id, context.get("member_id")))
        return {"action": action, "task_id": t.id, "offered": n}
    if action == "write_record":
        rec = models.Record(table_id=cfg["table_id"], group_id=group_id,
                            member_ref_id=cfg.get("member_ref_id") or context.get("member_id"),
                            data=cfg.get("data", {}),
                            created_by_member_id=context.get("member_id"))
        db.add(rec)
        return {"action": action, "table_id": cfg["table_id"]}
    if action == "run_agent":
        from app.agents_logic import run_agent
        agent = db.query(models.AgentDef).filter_by(id=cfg.get("agent_id"), group_id=group_id).first()
        if not agent:
            return {"action": action, "error": "agent not found"}
        run = run_agent(db, agent, input_query=cfg.get("query", context.get("note", "")),
                        target_member_id=cfg.get("member_id") or context.get("member_id"))
        return {"action": action, "agent_run_id": run.id}
    if action in ("inspect", "report"):
        return {"action": action, "note": cfg.get("note", "report generated")}
    return {"action": action, "note": "unknown action"}


def compute_view_rows(db, view):
    """Filter + project a table into human-readable rows."""
    from app.rules import _compare, _get
    if not view.source_table_id:
        return []
    recs = db.query(models.Record).filter_by(table_id=view.source_table_id).all()
    filt = view.filter or {}
    cols = view.columns or []
    rows = []
    for r in recs:
        d = r.data or {}
        if filt:
            conds = filt.get("and", [filt]) if isinstance(filt, dict) else []
            keep = True
            for f in conds:
                if not f.get("field"):
                    continue
                if not _compare(_get(d, f["field"]), f.get("op", "=="), f.get("value")):
                    keep = False
                    break
            if not keep:
                continue
        member = db.query(models.Membership).filter_by(id=r.member_ref_id).first() if r.member_ref_id else None
        row = {"_record_id": r.id,
               "_member": member.display_name if member else "—",
               "_created": r.created_at.isoformat() if r.created_at else ""}
        for c in cols:
            row[c] = d.get(c)
        rows.append(row)
    return rows
