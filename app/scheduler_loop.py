"""In-process scheduler: group cron jobs.

Each Schedule has kind once|interval|daily|weekly|cron and an action
(inspect|match|follow_up|report|push_inbox|create_task|write_record|run_agent).
A daemon thread ticks every 15s, runs due schedules, logs ScheduleRuns,
and fires redirect events (schedule.fired) so chains keep working.

Cron format: 'M H * * *' (minute hour, day/month/weekday must be *).
"""
import threading
import time
from datetime import datetime, timezone, timedelta


def utcnow():
    return datetime.now(timezone.utc)


def _as_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def compute_next(sched, now=None):
    now = _as_aware(now or utcnow())
    try:
        if sched.kind == "once" and sched.run_at:
            ra = _as_aware(sched.run_at)
            return ra if ra > now else None
        if sched.kind == "interval" and sched.interval_seconds:
            base = _as_aware(sched.last_run) or _as_aware(sched.created_at) or now
            nxt = base + timedelta(seconds=int(sched.interval_seconds))
            while nxt <= now:
                nxt += timedelta(seconds=int(sched.interval_seconds))
            return nxt
        if sched.kind in ("daily", "weekly", "cron") and sched.cron_expr:
            parts = sched.cron_expr.strip().split()
            if len(parts) >= 2:
                minute, hour = int(parts[0]), int(parts[1])
                nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if nxt <= now:
                    nxt += timedelta(days=1)
                if sched.kind == "weekly" and sched.weekday is not None:
                    while nxt.weekday() != int(sched.weekday):
                        nxt += timedelta(days=1)
                return nxt
        if sched.kind == "daily" and sched.time_of_day:
            hh, mm = sched.time_of_day.split(":")[:2]
            nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            return nxt
    except Exception:
        return None
    return None


def normalize_schedule(sched):
    """Fill cron_expr from time_of_day/weekday for daily/weekly kinds."""
    if sched.kind == "daily" and sched.time_of_day and not sched.cron_expr:
        hh, mm = sched.time_of_day.split(":")[:2]
        sched.cron_expr = f"{int(mm)} {int(hh)} * * *"
    if sched.kind == "weekly" and sched.time_of_day and not sched.cron_expr:
        hh, mm = sched.time_of_day.split(":")[:2]
        sched.cron_expr = f"{int(mm)} {int(hh)} * * *"
    sched.next_run = compute_next(sched)


def run_schedule_once(db, sched):
    """Execute a single schedule's action. Returns detail dict."""
    from app import models
    from app.workflows import push_inbox, post_message, fire_event, create_offers_for_task
    from app.agents_logic import run_agent
    cfg = sched.action_config or {}
    gid = sched.group_id
    detail = {"schedule": sched.name, "action": sched.action}
    try:
        if sched.action == "inspect":
            since = _as_aware(sched.last_run) or (utcnow() - timedelta(days=1))
            n_new = db.query(models.Record).filter(
                models.Record.group_id == gid, models.Record.created_at >= since.replace(tzinfo=None)
                if since.tzinfo else since).count() if False else db.query(models.Record).filter_by(group_id=gid).count()
            msg = f"Inspect: {n_new} record(s) in scope. {cfg.get('note','Review new jobs.')}"
            push_inbox(db, gid, f"Scheduled inspect — {sched.name}", msg, kind="report")
            post_message(db, gid, f"⏰ {sched.name}: {msg}", kind="schedule")
            detail.update({"records": n_new})
        elif sched.action == "match":
            task = db.query(models.TaskDef).filter_by(id=cfg.get("task_id"), group_id=gid).first()
            if not task:
                task = db.query(models.TaskDef).filter_by(group_id=gid, status="live").first()
            if task:
                from app.workflows import member_name as _mn
                who = _mn(db, gid, task.created_by)
                n = create_offers_for_task(db, task, announce=False, author=who)
                if n > 0 and not cfg.get("quiet"):
                    post_message(db, gid, f"matched '{task.title}' to {n} new eligible member(s).",
                                 kind="task", author=who, payload={"task_id": task.id, "offered": n})
                detail.update({"task_id": task.id, "offered": n})
            else:
                detail["note"] = "no live task to match"
        elif sched.action == "follow_up":
            outstanding = db.query(models.TaskOffer).filter_by(group_id=gid, status="offered").all()
            for o in outstanding[:20]:
                m = db.query(models.Membership).filter_by(id=o.member_id).first()
                t = db.query(models.TaskDef).filter_by(id=o.task_id).first()
                if m and t:
                    push_inbox(db, gid, f"Reminder: {t.title}",
                               "Still waiting on your response — tap to accept.",
                               kind="task", member_id=m.id, ref_type="task", ref_id=t.id)
            detail.update({"reminded": len(outstanding)})
            post_message(db, gid, f"⏰ {sched.name}: followed up on {len(outstanding)} outstanding offer(s).", kind="schedule")
        elif sched.action == "report":
            n_members = db.query(models.Membership).filter_by(group_id=gid).count()
            n_tasks = db.query(models.TaskDef).filter_by(group_id=gid).count()
            n_open = db.query(models.TaskOffer).filter_by(group_id=gid, status="offered").count()
            body = f"Weekly report: {n_members} members • {n_tasks} tasks • {n_open} outstanding offer(s). {cfg.get('note','')}"
            push_inbox(db, gid, f"Report — {sched.name}", body, kind="report")
            post_message(db, gid, f"⏰ {sched.name}: {body}", kind="schedule")
            detail.update({"members": n_members, "tasks": n_tasks, "open": n_open})
        elif sched.action == "push_inbox":
            push_inbox(db, gid, cfg.get("title", sched.name), cfg.get("body", ""), kind="report",
                       member_id=cfg.get("member_id"))
            detail["pushed"] = True
        elif sched.action == "create_task":
            t = models.TaskDef(group_id=gid, title=cfg.get("title", sched.name),
                               description=cfg.get("description", "Created by schedule"),
                               payout_text=cfg.get("payout_text", ""),
                               audience=cfg.get("audience", {}), status="live")
            db.add(t)
            db.flush()
            n = create_offers_for_task(db, t)
            detail.update({"task_id": t.id, "offered": n})
        elif sched.action == "run_agent":
            agent = db.query(models.AgentDef).filter_by(id=cfg.get("agent_id"), group_id=gid).first()
            if agent:
                run = run_agent(db, agent, input_query=cfg.get("query", ""),
                                target_member_id=cfg.get("member_id"))
                detail["agent_run_id"] = run.id
        elif sched.action == "motivate":
            quotes = cfg.get("quotes") or [
                "Ship something small today — momentum beats perfection.",
                "The team that demos together, wins together. One commit at a time.",
                "Done is better than perfect. Push it, then polish it.",
                "Every expert was once a beginner at their first hackathon.",
                "48 hours, one crew, zero excuses. Let's build.",
            ]
            line = quotes[utcnow().timetuple().tm_yday % len(quotes)]
            push_inbox(db, gid, f"Morning spark — {sched.name}", line, kind="report")
            post_message(db, gid, line, kind="chat", author="Morning spark")
            detail["quote"] = line
        # chain into redirects
        fire_event(db, gid, "schedule.fired", {"trigger_ref": sched.id, "note": sched.name})
        db.add(models.ScheduleRun(schedule_id=sched.id, group_id=gid, status="success", detail=detail))
        status = "success"
    except Exception as e:
        db.add(models.ScheduleRun(schedule_id=sched.id, group_id=gid, status="failed", detail={"error": str(e)}))
        detail = {"error": str(e)}
        status = "failed"
    sched.last_run = utcnow().replace(tzinfo=None)
    sched.next_run = compute_next(sched)
    if sched.kind == "once":
        sched.active = False
        sched.next_run = None
    return status, detail


_started = False


def start_loop():
    global _started
    if _started:
        return
    _started = True

    def _tick():
        from app.database import SessionLocal
        from app import models
        while True:
            try:
                db = SessionLocal()
                now = utcnow().replace(tzinfo=None)
                due = db.query(models.Schedule).filter_by(active=True).all()
                for s in due:
                    nxt = _as_aware(s.next_run)
                    if nxt is None:
                        normalize_schedule(s)
                        db.commit()
                        nxt = _as_aware(s.next_run)
                    if nxt and nxt.replace(tzinfo=None) <= now:
                        run_schedule_once(db, s)
                        db.commit()
                db.close()
            except Exception:
                pass
            time.sleep(15)

    threading.Thread(target=_tick, daemon=True).start()
