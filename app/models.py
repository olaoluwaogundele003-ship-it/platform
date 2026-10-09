"""SQLAlchemy models for Platform monolith.

Single-hub design: every group has Memberships (the hub). Up to 5 TableDefs
per group (including the implicit members hub mirror). Every Record carries
member_ref_id pointing at the hub row.
"""
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, Float,
    ForeignKey, JSON, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import secrets
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


def invite_code():
    return secrets.token_hex(3).upper()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    email = Column(String(160), unique=True, nullable=False)
    username = Column(String(64), unique=True, nullable=True)
    password_hash = Column(String(256), default="")
    created_at = Column(DateTime, default=utcnow)


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    description = Column(Text, default="")
    workspace = Column(String(160), default="Northstar Workspace")
    invite_code = Column(String(16), default=invite_code, unique=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    memberships = relationship("Membership", back_populates="group", cascade="all, delete-orphan")
    tables = relationship("TableDef", back_populates="group", cascade="all, delete-orphan")


class Membership(Base):
    """The hub row. Every entry any member makes references this row."""
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    display_name = Column(String(160), nullable=False)
    role = Column(String(32), default="member")  # admin | member
    tags = Column(JSON, default=list)            # e.g. ["eligible-drivers", "bike"]
    profile = Column(JSON, default=dict)         # flexible: city, rating, verification, ...
    prefs = Column(JSON, default=dict)           # per-member settings, e.g. {"phased": {"enabled": true, "gap": 1500}}
    joined_at = Column(DateTime, default=utcnow)

    group = relationship("Group", back_populates="memberships")

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_member_user"),)


class TableDef(Base):
    __tablename__ = "tables"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(64), nullable=False)   # slug
    name = Column(String(120), nullable=False)
    description = Column(Text, default="")
    is_hub = Column(Boolean, default=False)     # members directory mirror
    columns = Column(JSON, default=list)        # [{name,label,type,required,options}]
    created_at = Column(DateTime, default=utcnow)

    group = relationship("Group", back_populates="tables")


class Record(Base):
    __tablename__ = "records"
    id = Column(Integer, primary_key=True)
    table_id = Column(Integer, ForeignKey("tables.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_ref_id = Column(Integer, ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True, index=True)
    data = Column(JSON, default=dict)
    created_by_member_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Rule(Base):
    """Natural-language rule compiled once into a deterministic check list."""
    __tablename__ = "rules"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    natural_language = Column(Text, nullable=False)
    compiled = Column(JSON, default=dict)       # {"checks":[...], "needs_review":bool}
    explanation = Column(Text, default="")      # shown back to creator for confirmation
    status = Column(String(16), default="draft")  # draft | confirmed
    scope = Column(String(32), default="general")  # form | task | table | agent | general
    scope_ref = Column(Integer, nullable=True)
    ai_model = Column(String(120), default="")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class FormDef(Base):
    __tablename__ = "forms"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    table_id = Column(Integer, ForeignKey("tables.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, default="")
    fields = Column(JSON, default=list)  # [{name,label,type,required,options}]
    rule_id = Column(Integer, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), default="published")
    created_at = Column(DateTime, default=utcnow)


class FormSubmission(Base):
    __tablename__ = "form_submissions"
    id = Column(Integer, primary_key=True)
    form_id = Column(Integer, ForeignKey("forms.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True)
    data = Column(JSON, default=dict)
    status = Column(String(16), default="accepted")  # accepted | rejected
    reason = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)


class TaskDef(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    payout_text = Column(String(120), default="")
    rule_id = Column(Integer, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True)
    audience = Column(JSON, default=dict)  # {"tags":[...], "filter":{"field","op","value"}}
    write_table_id = Column(Integer, nullable=True)
    write_template = Column(JSON, default=dict)
    status = Column(String(16), default="live")  # live | draft | closed
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class TaskOffer(Base):
    __tablename__ = "task_offers"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), default="offered")  # offered|accepted|declined|completed
    response_text = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)
    responded_at = Column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(160), default="Platform")
    kind = Column(String(24), default="chat")  # chat|system|task|form|query|agent|schedule|status
    body = Column(Text, default="")
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow)


class MessageReaction(Base):
    """Emoji reactions. Limited set enforced in API + UI."""
    __tablename__ = "message_reactions"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False)
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime, default=utcnow)


class InboxItem(Base):
    __tablename__ = "inbox"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="CASCADE"), nullable=True, index=True)  # null = public
    title = Column(String(200), nullable=False)
    body = Column(Text, default="")
    kind = Column(String(32), default="info")  # task|form|status|agent|report|offer|system
    ref_type = Column(String(32), default="")
    ref_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class PageView(Base):
    __tablename__ = "views"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, default="")
    source_table_id = Column(Integer, ForeignKey("tables.id", ondelete="SET NULL"), nullable=True)
    columns = Column(JSON, default=list)
    filter = Column(JSON, default=dict)  # {"field","op","value"} or {"and":[...]}
    template = Column(String(64), default="table")  # job_board|roster|dispatch_board|funnel|table
    is_shared = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class ViewSnapshot(Base):
    """Static published page of a view — one frozen data snapshot + external link."""
    __tablename__ = "view_snapshots"
    id = Column(Integer, primary_key=True)
    view_id = Column(Integer, ForeignKey("views.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(32), unique=True, nullable=False, index=True)
    title = Column(String(200), default="")
    html = Column(Text, default="")
    row_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class Redirect(Base):
    """Workflow edge: when trigger_event fires, run action. Chains tasks."""
    __tablename__ = "redirects"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(180), nullable=False)
    trigger_event = Column(String(64), nullable=False)  # task.completed|task.accepted|form.submitted|status.confirmed|schedule.fired|member.joined|offer.accepted
    trigger_ref = Column(Integer, nullable=True)  # optional task_id/form_id/schedule_id
    action = Column(String(32), nullable=False)  # create_task|push_inbox|write_record|run_agent|push_public
    action_config = Column(JSON, default=dict)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class Schedule(Base):
    """Group-level cron jobs. What jobs normally do, but owned by the group."""
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(180), nullable=False)
    description = Column(Text, default="")
    kind = Column(String(16), default="interval")  # once|interval|daily|weekly|cron
    run_at = Column(DateTime, nullable=True)       # for once
    interval_seconds = Column(Integer, nullable=True)  # for interval
    cron_expr = Column(String(64), default="")     # for cron/daily/weekly (normalized)
    time_of_day = Column(String(8), default="")    # HH:MM for daily/weekly
    weekday = Column(Integer, nullable=True)       # 0=Mon for weekly
    action = Column(String(32), default="report")  # inspect|match|follow_up|report|push_inbox|create_task|write_record|run_agent
    action_config = Column(JSON, default=dict)
    active = Column(Boolean, default=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class ScheduleRun(Base):
    __tablename__ = "schedule_runs"
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(16), default="success")
    detail = Column(JSON, default=dict)
    ran_at = Column(DateTime, default=utcnow)


class AgentDef(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    kind = Column(String(40), default="analyst")  # analyst|job_finder|compliance|tutor_matcher|delivery_matcher|web_search
    description = Column(Text, default="")
    config = Column(JSON, default=dict)  # sources, match_rules, permissions
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    target_member_id = Column(Integer, ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True)
    input_query = Column(Text, default="")
    result_items = Column(JSON, default=list)
    proposed_mutation = Column(JSON, default=dict)  # {table_key, rows:[...]} or {member_update:{...}}
    status = Column(String(16), default="proposed")  # proposed|confirmed|applied|rejected
    validation_note = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class ExternalEvent(Base):
    """In-app browser outcome: payment/booking/QR handoff reported back into schema."""
    __tablename__ = "external_events"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(40), default="payment")  # payment|booking|form|qr_handoff|license_check
    ref_label = Column(String(200), default="")
    url = Column(String(500), default="")
    status = Column(String(16), default="pending")  # pending|confirmed|failed
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
