"""Seed demo workspace: Driver Network (Crowd-style) + Study Group.

Covers every primitive: schema(5), tasks, forms, rules, inbox, views,
redirects, schedules, agents, external status, chat.
"""
from app import models
from app.scheduler_loop import normalize_schedule


DEMO_PASSWORD = "platform"
DEMO_USERS = [
    ("alex", "Alex Rowan", "alex@platform.local"),
    ("maya", "Maya Kim", "maya@platform.local"),
    ("jon", "Jon Okafor", "jon@platform.local"),
    ("sofia", "Sofia Nwosu", "sofia@platform.local"),
]


def ensure_demo_credentials(db):
    """Backfill usernames/passwords on pre-auth databases; idempotent.

    Never creates users — accounts are real people who register themselves.
    """
    from app.auth import hash_password
    for username, name, email in DEMO_USERS:
        u = db.query(models.User).filter_by(email=email).first() or \
            db.query(models.User).filter_by(username=username).first()
        if not u:
            continue
        changed = False
        if not u.username:
            u.username = username
            changed = True
        if not u.password_hash:
            u.password_hash = hash_password(DEMO_PASSWORD)
            changed = True
        if changed:
            db.flush()
        # link hub memberships that were created before users existed
        mem = db.query(models.Membership).filter_by(display_name=name).first()
        if mem and not mem.user_id:
            mem.user_id = u.id
    db.commit()


def migrate(db):
    """One-off fixes for databases seeded before a design change. Idempotent."""
    # stake rule belongs on acceptance, not on the sender listing form
    for f in db.query(models.FormDef).filter_by(title="List a package").all():
        rule = db.query(models.Rule).filter_by(id=f.rule_id).first() if f.rule_id else None
        if rule and "stake" in (rule.name or "").lower():
            f.rule_id = None
    db.commit()


def seed(db):
    if db.query(models.Group).first():
        ensure_demo_credentials(db)
        return None
    from app.auth import hash_password
    # users
    alex = models.User(name="Alex Rowan", email="alex@platform.local", username="alex",
                       password_hash=hash_password(DEMO_PASSWORD))
    maya = models.User(name="Maya Kim", email="maya@platform.local", username="maya",
                       password_hash=hash_password(DEMO_PASSWORD))
    jon = models.User(name="Jon Okafor", email="jon@platform.local", username="jon",
                      password_hash=hash_password(DEMO_PASSWORD))
    sofia = models.User(name="Sofia Nwosu", email="sofia@platform.local", username="sofia",
                        password_hash=hash_password(DEMO_PASSWORD))
    db.add_all([alex, maya, jon, sofia])
    db.flush()

    g = models.Group(name="Driver Network", description="Onboard, coordinate and dispatch the driver community.",
                     workspace="Northstar Workspace", created_by=alex.id)
    db.add(g)
    db.flush()

    def member(user, name, role, tags, profile):
        m = models.Membership(group_id=g.id, user_id=user.id if user else None,
                              display_name=name, role=role, tags=tags, profile=profile)
        db.add(m)
        db.flush()
        return m

    m_alex = member(alex, "Alex Rowan", "admin", ["ops"], {"city": "Lagos", "rating": 5.0, "verification": "Verified", "availability": "Available", "role": "admin"})
    m_maya = member(maya, "Maya Kim", "admin", ["ops"], {"city": "Lagos", "rating": 4.9, "verification": "Verified", "availability": "Available", "skills": ["dispatch"]})
    m_jon = member(jon, "Jon Okafor", "member", ["dispatch"], {"city": "Lagos", "rating": 4.8, "verification": "Verified", "availability": "Available"})
    m_sofia = member(sofia, "Sofia Nwosu", "member", ["compliance"], {"city": "Lagos", "rating": 4.9, "verification": "Verified", "availability": "Available"})
    drivers = [
        ("Samuel Adeyemi", ["eligible-drivers", "car"], {"city": "Lagos", "rating": 4.92, "verification": "Verified", "availability": "Available", "transport_mode": "car", "stake_balance": 50000, "phone": "+234 802 044 118", "fleet": "Airport fleet"}),
        ("Tola Bello", ["eligible-drivers", "car"], {"city": "Lagos", "rating": 4.81, "verification": "Verified", "availability": "Available", "transport_mode": "car", "stake_balance": 30000, "phone": "+234 811 392 026", "fleet": "Premium"}),
        ("Kelechi Nnaji", ["city-fleet", "bike"], {"city": "Ikeja", "rating": 4.74, "verification": "In review", "availability": "On shift", "transport_mode": "bike", "stake_balance": 12000, "phone": "+234 703 882 490", "fleet": "City fleet"}),
        ("Fatima Abdul", ["premium", "car"], {"city": "Lagos", "rating": 4.96, "verification": "Verified", "availability": "Unavailable", "transport_mode": "car", "stake_balance": 80000, "phone": "+234 809 441 750", "fleet": "Premium"}),
        ("Emeka Obi", ["city-fleet", "bike"], {"city": "Yaba", "rating": 4.68, "verification": "Missing docs", "availability": "Available", "transport_mode": "bike", "stake_balance": 8000, "phone": "+234 706 554 103", "fleet": "City fleet"}),
        ("Rita Abiola", ["eligible-drivers", "car"], {"city": "Lekki", "rating": 4.88, "verification": "Verified", "availability": "Available", "transport_mode": "car", "stake_balance": 45000, "phone": "+234 815 339 207", "fleet": "Airport fleet"}),
    ]
    m_drivers = [member(None, n, "member", t, p) for n, t, p in drivers]

    # tables (5 incl hub mirror)
    hub = models.TableDef(group_id=g.id, key="members", name="Members", description="Group directory (hub).", is_hub=True,
                          columns=[{"name": "name", "label": "Name", "type": "text", "required": True},
                                   {"name": "fleet", "label": "Classification", "type": "select", "options": ["Airport fleet", "Premium", "City fleet"]},
                                   {"name": "verification", "label": "Verification", "type": "select", "options": ["Verified", "In review", "Missing docs"]},
                                   {"name": "rating", "label": "Rating", "type": "number"},
                                   {"name": "city", "label": "City", "type": "text"},
                                   {"name": "availability", "label": "Availability", "type": "select", "options": ["Available", "On shift", "Unavailable"]}])
    vehicles = models.TableDef(group_id=g.id, key="vehicles", name="Vehicles", description="Fleet vehicles.",
                               columns=[{"name": "plate", "label": "Plate", "type": "text", "required": True}, {"name": "type", "label": "Type", "type": "text"}, {"name": "status", "label": "Status", "type": "text"}])
    dispatches = models.TableDef(group_id=g.id, key="dispatches", name="Dispatches", description="Jobs & package listings.",
                                 columns=[{"name": "title", "label": "Title", "type": "text", "required": True}, {"name": "value", "label": "Value ₦", "type": "number"}, {"name": "route", "label": "Route", "type": "text"}, {"name": "status", "label": "Status", "type": "select", "options": ["open", "accepted", "in_transit", "delivered"]}, {"name": "courier", "label": "Courier", "type": "text"}])
    documents = models.TableDef(group_id=g.id, key="documents", name="Documents", description="Verification docs.",
                                columns=[{"name": "doc_type", "label": "Type", "type": "text"}, {"name": "status", "label": "Status", "type": "text"}])
    payments = models.TableDef(group_id=g.id, key="payments", name="Payments", description="Payouts & escrow.",
                               columns=[{"name": "amount", "label": "Amount ₦", "type": "number"}, {"name": "to", "label": "To", "type": "text"}, {"name": "status", "label": "Status", "type": "text"}])
    db.add_all([hub, vehicles, dispatches, documents, payments])
    db.flush()

    # records
    db.add_all([
        models.Record(table_id=vehicles.id, group_id=g.id, member_ref_id=m_drivers[0].id, data={"plate": "LAG-441-KJ", "type": "Sedan", "status": "passed"}),
        models.Record(table_id=dispatches.id, group_id=g.id, member_ref_id=m_jon.id, data={"title": "Airport run — MMA2", "value": 25000, "route": "Lekki → MMA2", "status": "open", "courier": ""}),
        models.Record(table_id=dispatches.id, group_id=g.id, member_ref_id=m_jon.id, data={"title": "Parcel — Ikeja", "value": 9000, "route": "Yaba → Ikeja", "status": "open", "courier": ""}),
        models.Record(table_id=payments.id, group_id=g.id, member_ref_id=m_drivers[0].id, data={"amount": 10800, "to": "Samuel Adeyemi", "status": "released"}),
    ])

    # rules (compiled once, confirmed)
    r1 = models.Rule(group_id=g.id, name="Airport eligibility",
                     natural_language="Only verified drivers with rating above 4.7 can accept airport shifts",
                     compiled={"checks": [
                         {"field": "verification", "op": "in", "value": ["Verified", "active"], "source": "profile", "description": "member must be verified/active"},
                         {"field": "rating", "op": ">", "value": 4.7, "source": "profile", "description": "rating > 4.7"}],
                         "needs_review": False},
                     explanation="IF [verified/active only] AND [rating > 4.7]", status="confirmed", scope="task")
    r2 = models.Rule(group_id=g.id, name="Stake coverage",
                     natural_language="Package value must never exceed the courier's available stake",
                     compiled={"checks": [
                         {"field": "value", "op": "<=", "value": "$profile.stake_balance", "source": "cross", "description": "package value must be ≤ courier's available stake"}],
                         "needs_review": False},
                     explanation="IF [package value ≤ courier's available stake]", status="confirmed", scope="task")
    db.add_all([r1, r2])
    db.flush()

    # forms
    f1 = models.FormDef(group_id=g.id, table_id=hub.id, title="Driver verification", description="Identity, license and vehicle details.",
                        fields=[{"name": "full_name", "label": "Full legal name", "type": "text", "required": True},
                                {"name": "license_number", "label": "Driver license number", "type": "text", "required": True},
                                {"name": "fleet", "label": "Vehicle category", "type": "select", "options": ["Airport fleet", "Premium", "City fleet"]},
                                {"name": "city", "label": "Operating city", "type": "text"}], rule_id=None, status="published")
    f2 = models.FormDef(group_id=g.id, table_id=dispatches.id, title="List a package", description="Sender lists value, route and price.",
                        fields=[{"name": "title", "label": "Package title", "type": "text", "required": True},
                                {"name": "value", "label": "Value ₦", "type": "number", "required": True},
                                {"name": "route", "label": "Route", "type": "text", "required": True}], rule_id=None, status="published")
    # NOTE: the stake-coverage rule lives on task *acceptance* (courier side),
    # not on the sender's listing form — anyone may list, only covered
    # couriers may accept.
    db.add_all([f1, f2])
    db.flush()

    # tasks
    t1 = models.TaskDef(group_id=g.id, title="Airport Dispatch Shift", description="Tomorrow · 6:00–11:00 AM airport pickups.",
                        payout_text="$108 payout", rule_id=r1.id, audience={"tags": ["eligible-drivers"]}, status="live", created_by=m_jon.id)
    t2 = models.TaskDef(group_id=g.id, title="Request document review", description="Ask an admin to resolve a document mismatch.",
                        payout_text="", rule_id=None, audience={}, status="live", created_by=m_sofia.id)
    t3 = models.TaskDef(group_id=g.id, title="Accept parcel — Ikeja", description="₦9,000 parcel, bike-friendly. Stake-covered only.",
                        payout_text="₦9,000", rule_id=r2.id, audience={}, status="live", created_by=m_jon.id)
    db.add_all([t1, t2, t3])
    db.flush()

    from app.workflows import create_offers_for_task
    create_offers_for_task(db, t1)
    create_offers_for_task(db, t2)
    # accept one to show state
    from app.models import TaskOffer
    o = db.query(TaskOffer).filter_by(task_id=t1.id).first()
    if o:
        o.status = "accepted"

    # chat
    db.add_all([
        models.Message(group_id=g.id, member_id=m_maya.id, author_name="Maya Kim", kind="chat", body="Morning everyone. Verification finished overnight — 18 drivers ready for dispatch, six waiting on documents."),
        models.Message(group_id=g.id, member_id=m_alex.id, author_name="Alex Rowan", kind="chat", body="Can we offer tomorrow's airport shift only to verified drivers nearby with rating above 4.7?"),
        models.Message(group_id=g.id, author_name="Platform", kind="system", body="Platform checked Members and Availability — 12 people match."),
        models.Message(group_id=g.id, member_id=m_jon.id, author_name="Jon Okafor", kind="task", body="Added it. @eligible-drivers, let us know before noon.", payload={"task_id": t1.id}),
    ])

    # inbox
    db.add_all([
        models.InboxItem(group_id=g.id, member_id=None, title="Verification complete", body="18 drivers in Lagos passed document review.", kind="status"),
        models.InboxItem(group_id=g.id, member_id=m_drivers[0].id, title="Task: Airport Dispatch Shift", body="You're eligible — tap to accept before noon.", kind="task", ref_type="task", ref_id=t1.id),
        models.InboxItem(group_id=g.id, member_id=m_drivers[4].id, title="Action needed: documents", body="Your license photo is blurry — resubmit via the verification form.", kind="form", ref_type="form", ref_id=f1.id),
    ])

    # views
    db.add_all([
        models.PageView(group_id=g.id, title="Dispatch board", description="Live work grouped by status.", source_table_id=dispatches.id, columns=["title", "value", "route", "status", "courier"], filter={}, template="dispatch_board"),
        models.PageView(group_id=g.id, title="Verification funnel", description="Onboarding progress.", source_table_id=hub.id, columns=["name", "fleet", "verification", "rating"], filter={}, template="funnel"),
        models.PageView(group_id=g.id, title="Eligible airport drivers", description="rating 4.7+ verified.", source_table_id=hub.id, columns=["name", "rating", "city"], filter={"field": "rating", "op": ">", "value": 4.7}, template="roster"),
    ])

    # redirects (workflow chains)
    db.add_all([
        models.Redirect(group_id=g.id, name="Verification → compliance check", trigger_event="form.submitted", trigger_ref=f1.id,
                        action="run_agent", action_config={"agent_id": None, "query": "verify license"}),
        models.Redirect(group_id=g.id, name="Accept → request QR handoff", trigger_event="offer.accepted", trigger_ref=t1.id,
                        action="push_inbox", action_config={"title": "Next: QR handoff", "body": "Scan the pickup QR to confirm handoff — escrow releases automatically."}),
        models.Redirect(group_id=g.id, name="Payment confirmed → receipt", trigger_event="status.confirmed",
                        action="push_public", action_config={"title": "Payment received", "body": "Payment received · receipt saved to record."}),
    ])

    # agents
    a1 = models.AgentDef(group_id=g.id, name="Platform analyst", kind="analyst", description="Answers questions and proposes structured changes.", config={"permissions": ["read", "suggest"]})
    a2 = models.AgentDef(group_id=g.id, name="Compliance search", kind="compliance", description="Checks licenses against external sources.", config={"permissions": ["search", "update"]})
    a3 = models.AgentDef(group_id=g.id, name="Job-finding agent", kind="job_finder", description="Matches profiles to external opportunities.", config={"sources": ["job boards"], "permissions": ["read", "propose"]})
    a4 = models.AgentDef(group_id=g.id, name="Web search", kind="web_search", description="General external lookup.", config={})
    db.add_all([a1, a2, a3, a4])
    db.flush()
    # point redirect at compliance agent
    db.query(models.Redirect).filter_by(name="Verification → compliance check").first().action_config = {"agent_id": a2.id, "query": "verify license"}

    # schedules — the group's cron jobs
    s1 = models.Schedule(group_id=g.id, name="07:00 Inspect — review new jobs", description="Review new jobs", kind="daily", time_of_day="07:00", action="inspect", action_config={"note": "Review new jobs"}, created_by=m_alex.id)
    s2 = models.Schedule(group_id=g.id, name="12:00 Match — find eligible members", description="Find eligible members", kind="daily", time_of_day="12:00", action="match", action_config={"task_id": t1.id}, created_by=m_alex.id)
    s3 = models.Schedule(group_id=g.id, name="17:00 Follow up — outstanding tasks", description="Check outstanding tasks", kind="daily", time_of_day="17:00", action="follow_up", action_config={}, created_by=m_alex.id)
    s4 = models.Schedule(group_id=g.id, name="Weekly report", description="Notify responsible people", kind="weekly", time_of_day="09:00", weekday=0, action="report", action_config={"note": "Notify responsible people"}, created_by=m_alex.id)
    for s in (s1, s2, s3, s4):
        normalize_schedule(s)
        db.add(s)
    db.flush()

    # second group: campus (light)
    g2 = models.Group(name="Campus Study Group", description="Exam prep coordination.", workspace="Northstar Workspace", created_by=alex.id)
    db.add(g2)
    db.flush()
    db.add(models.Membership(group_id=g2.id, user_id=alex.id, display_name="Alex Rowan", role="admin", tags=["math-strong"], profile={"city": "Yaba", "skills": ["math"], "availability": "Available", "verification": "Verified"}))

    db.commit()
    return g
