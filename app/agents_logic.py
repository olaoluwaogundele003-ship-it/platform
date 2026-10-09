"""Schema-aware agents: Query -> Act -> Return -> Mutate.

Agents read the group schema, perform an external action (search/lookup/match),
return a JSON payload shaped for the schema, and only write after the member
confirms AND the group's validation rules pass.
"""
from app import models
from app.rules import evaluate
from app import ai as ai_mod


MOCK_JOBS = [
    {"title": "Junior PHP Developer", "detail": "Lagos • full time • ₦180k/mo", "skills": ["php", "sql"], "score": 82},
    {"title": "Backend Intern", "detail": "Remote • entry level • stipend", "skills": ["php", "sql", "laravel"], "score": 76},
    {"title": "Laravel Developer", "detail": "Lagos • full time • ₦250k/mo", "skills": ["php", "laravel"], "score": 71},
    {"title": "Dispatch Rider", "detail": "Ikeja • per-delivery pay", "skills": ["driving"], "score": 64},
    {"title": "Python Tutor", "detail": "Yaba • weekends", "skills": ["python"], "score": 69},
]


def _profile_of(db, member_id):
    m = db.query(models.Membership).filter_by(id=member_id).first() if member_id else None
    return m


def analyst_query(db, group_id, nl):
    """Local analyst: compile NL into a member filter + stats (AI-assisted view planning)."""
    from app import ai as ai_mod
    members = db.query(models.Membership).filter_by(group_id=group_id).all()
    t = (nl or "").lower()
    filt = {}
    if "verified" in t:
        filt = {"field": "verification", "op": "in", "value": ["Verified", "active"]}
    if "lagos" in t:
        filt = {"and": [filt, {"field": "city", "op": "==", "value": "Lagos"}]} if filt else {"field": "city", "op": "==", "value": "Lagos"}
    if "4.7" in t or "rating" in t:
        r = {"field": "rating", "op": ">", "value": 4.7}
        filt = {"and": [filt, r]} if filt else r
    # AI view planner refines when available
    try:
        snap = f"members({len(members)}): verification, rating, city, availability, skills"
        planned, _ = ai_mod.ai_plan_view(nl, snap)
        if planned and planned.get("filter"):
            filt = planned["filter"]
    except Exception:
        pass
    from app.rules import member_matches_filter
    matched = [m for m in members if member_matches_filter(m.profile or {}, m.tags or [], filt)] if filt else members
    ratings = [float((m.profile or {}).get("rating", 0) or 0) for m in matched if (m.profile or {}).get("rating")]
    return {
        "matched_ids": [m.id for m in matched],
        "count": len(matched),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
        "filter": filt,
    }


def _role_for(profile, membership):
    if (membership.role or "") == "admin":
        return "Team captain — architecture & final demo"
    skills = " ".join(profile.get("skills", []) if isinstance(profile.get("skills"), list) else str(profile.get("skills", "")).split(",")).lower()
    if "backend" in skills or "python" in skills or "api" in skills:
        return "Backend engineer — APIs & data"
    if "design" in skills or "figma" in skills or "frontend" in skills or "css" in skills:
        return "Product designer — UI & demo polish"
    if "pitch" in skills or "speak" in skills or "present" in skills:
        return "Pitch lead — story & presentation"
    if "mobile" in skills:
        return "Mobile engineer — demo app"
    return "Builder — features & integration"


def _profile_line(profile):
    bits = []
    for k in ("skills", "transport_mode", "city", "availability"):
        v = profile.get(k)
        if v:
            bits.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
    return "; ".join(bits) or "profile on file"


def run_agent(db, agent, input_query="", target_member_id=None):
    """Query -> Act -> Return. Creates an AgentRun in proposed state + inbox proposal."""
    kind = agent.kind
    group_id = agent.group_id
    members = db.query(models.Membership).filter_by(group_id=group_id).all()
    target = _profile_of(db, target_member_id) if target_member_id else None

    items, mutation, note = [], {}, ""
    if kind == "job_finder" and target:
        prof = target.profile or {}
        skills = [s.lower() for s in (prof.get("skills", []) if isinstance(prof.get("skills"), list) else str(prof.get("skills", "")).split(","))]
        # AI brain first
        ai_items, _ = ai_mod.ai_agent_brain(kind, prof, input_query)
        if ai_items:
            items = [{"title": i.get("title", "Opportunity"), "detail": i.get("detail", ""),
                      "score": i.get("score", 70)} for i in ai_items]
        else:
            for j in MOCK_JOBS:
                overlap = len(set(skills) & set(j["skills"])) if skills else 0
                score = j["score"] if overlap else max(40, j["score"] - 25)
                items.append({"title": j["title"], "detail": j["detail"], "score": score})
            items.sort(key=lambda x: -x["score"])
            items = items[:3]
        # external reach: live search blended in
        try:
            ext = ai_mod.web_search(f"{' '.join(skills[:3])} jobs {prof.get('city', 'Lagos')}", count=2)
            for e in ext[:1]:
                if "External result" not in e.get("title", ""):
                    items.append({"title": e["title"][:80], "detail": (e.get("detail", "")[:120] + f" ({e.get('url','')})"), "score": 60})
        except Exception:
            pass
        mutation = {"inbox_kind": "offer"}
        note = f"Matched {len(items)} opportunities to {target.display_name}'s profile."
    elif kind == "compliance":
        cands = [m for m in members if (m.profile or {}).get("verification") in ("In review", "Missing docs", None)]
        items = [{"title": f"Review {m.display_name}", "detail": f"License check pending • {(m.profile or {}).get('city','—')}", "score": 90} for m in cands[:5]]
        mutation = {"member_updates": [{"member_id": m.id, "profile_patch": {"verification": "Verified"}} for m in cands[:1]]}
        note = f"Found {len(cands)} member(s) awaiting document review."
    elif kind == "tutor_matcher":
        items = [{"title": f"Offer to tutor: Topic X, Tue 4pm?", "detail": f"Suggested for {m.display_name}", "score": 75} for m in members[:3]]
        mutation = {"table_key": "sessions", "rows": [{"tutor": m.display_name, "topic": "Topic X", "when": "Tue 4pm"} for m in members[:1]]}
        note = "Matched strong/weak topics into a suggested session."
    elif kind == "delivery_matcher":
        items = []
        for m in members:
            stake = float((m.profile or {}).get("stake_balance", 0) or 0)
            if stake > 0:
                items.append({"title": f"Courier {m.display_name}", "detail": f"stake ₦{stake:,.0f} • {(m.profile or {}).get('transport_mode','—')}", "score": min(95, int(stake / 1000))})
        items.sort(key=lambda x: -x["score"])
        mutation = {"table_key": "dispatches", "rows": []}
        note = f"{len(items)} courier(s) with stake coverage."
    elif kind == "web_search":
        results = ai_mod.web_search(input_query or "logistics Lagos", count=5)
        items = [{"title": r["title"], "detail": r["detail"], "score": 65} for r in results]
        mutation = {}
        note = f"External search returned {len(items)} result(s)."
    elif kind == "team_matcher":
        # hackathon flow: acceptors of a check-in task get role inboxes from their details
        task_id = (agent.config or {}).get("task_id")
        offers = [o for o in db.query(models.TaskOffer).filter_by(task_id=task_id, status="accepted").all()] if task_id else []
        if not offers:
            offers = [o for o in db.query(models.TaskOffer).filter_by(group_id=group_id, status="accepted").all()][:8]
        from app.workflows import push_inbox as _push
        items = []
        for o in offers:
            m = db.query(models.Membership).filter_by(id=o.member_id).first()
            if not m:
                continue
            role = _role_for(m.profile or {}, m)
            items.append({"title": m.display_name, "detail": role, "score": 90,
                          "member_id": m.id})
            _push(db, group_id, f"Your hackathon role: {role}",
                   f"Based on the details you already gave ({_profile_line(m.profile or {})}). See you there!",
                   kind="agent", member_id=m.id, ref_type="agent_run", ref_id=None)
        mutation = {"roles": [{"member_id": i["member_id"], "role": i["detail"]} for i in items]}
        note = f"Assigned roles to {len(items)} teammate(s) who checked in."
    else:  # analyst
        q = analyst_query(db, group_id, input_query or "verified")
        items = [{"title": f"{q['count']} member(s) match", "detail": f"avg rating {q['avg_rating']} • filter {q['filter']}", "score": 88}]
        mutation = {"filter": q["filter"], "matched_ids": q["matched_ids"]}
        note = "Analyst summarized current schema state."

    run = models.AgentRun(agent_id=agent.id, group_id=group_id,
                          target_member_id=target_member_id,
                          input_query=input_query or "",
                          result_items=items, proposed_mutation=mutation,
                          status="proposed", validation_note=note)
    db.add(run)
    db.flush()
    # Return phase: push proposal to inbox (personal if targeted else public)
    from app.workflows import push_inbox
    push_inbox(db, group_id, f"Agent '{agent.name}' proposal",
               f"{note} Tap Agents → Confirm to apply.".strip(),
               kind="agent", member_id=target_member_id,
               ref_type="agent_run", ref_id=run.id)
    return run


def _relevant(compiled, mode, keys):
    """Only enforce checks that touch the mutation at hand.

    Member-profile updates must not be blocked by package-value style
    cross checks; table rows must not be blocked by pure profile checks
    (e.g. rating) that the row cannot satisfy.
    """
    checks = (compiled or {}).get("checks", []) if isinstance(compiled, dict) else []
    keys = {str(k).lower() for k in (keys or [])}
    out = []
    for c in checks:
        src = c.get("source", "profile")
        field = str(c.get("field", "")).lower()
        val = c.get("value")
        if mode == "profile":
            # a profile patch is only responsible for the fields it touches
            if src == "profile" and field in keys:
                out.append(c)
        else:  # row mode: submission or cross checks referencing a present field
            if src in ("submission", "cross") and (field in keys or "value" in keys or "price" in keys):
                out.append(c)
    return {"checks": out, "needs_review": False}


def confirm_agent_run(db, run):
    """Mutate phase: validate proposed mutation against group rules, then write."""
    agent = db.query(models.AgentDef).filter_by(id=run.agent_id).first()
    group_id = run.group_id
    rules = db.query(models.Rule).filter_by(group_id=group_id, status="confirmed").all()
    mut = run.proposed_mutation or {}
    # validate member updates (profile-scope checks only)
    for upd in (mut.get("member_updates") or []):
        m = db.query(models.Membership).filter_by(id=upd.get("member_id")).first()
        if not m:
            continue
        new_profile = {**(m.profile or {}), **(upd.get("profile_patch", {}))}
        for r in rules:
            scoped = _relevant(r.compiled, "profile", upd.get("profile_patch", {}).keys())
            if not scoped["checks"]:
                continue
            ok, reasons = evaluate(scoped, new_profile, {})
            if not ok:
                run.status = "rejected"
                run.validation_note = f"Blocked by '{r.name}': " + "; ".join(reasons)
                return run
        m.profile = new_profile
    # validate table rows (row-scope checks only)
    if mut.get("table_key") and mut.get("rows"):
        table = db.query(models.TableDef).filter_by(group_id=group_id, key=mut["table_key"]).first()
        if table:
            for row in mut["rows"][:20]:
                for r in rules:
                    scoped = _relevant(r.compiled, "row", row.keys())
                    if not scoped["checks"]:
                        continue
                    ok, reasons = evaluate(scoped, {}, row)
                    if not ok:
                        run.status = "rejected"
                        run.validation_note = f"Blocked by '{r.name}': " + "; ".join(reasons)
                        return run
            for row in mut["rows"][:20]:
                db.add(models.Record(table_id=table.id, group_id=group_id,
                                     member_ref_id=run.target_member_id, data=row,
                                     created_by_member_id=run.target_member_id))
    from app.workflows import post_message
    run.status = "applied"
    run.validation_note = (run.validation_note or "") + " ✓ validated & applied."
    post_message(db, group_id, f"proposal applied: {run.validation_note[:140]}", kind="agent",
                 author=agent.name if agent else "Agent",
                 payload={"agent_run_id": run.id, "agent": agent.name if agent else ""})
    return run
