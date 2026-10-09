# platform

> **Platform — a group chat that organizes itself (FastAPI monolith)**

One process serves the API **and** the UI. SQLite storage, in-process scheduler,
AI-assisted (but deterministic) rules, schema-aware agents.

## Run

```bash
py -m pip install -r requirements.txt
set PLATFORM_AI_KEY=YOUR_PLATFORM_AI_KEY
py -m uvicorn app.main:app --port 8099
```

Open http://127.0.0.1:8099 and log in. Demo accounts: `alex` / `maya` /
`jon` / `sofia`, password `platform`. Or register a new username — then create
a group or join one with an invite code (`/?join=CODE` links work too).

## Auth & membership (standard)

- Register / login / logout with bearer tokens (30-day expiry, stored hashed
  via PBKDF2). Every `/api/*` endpoint except health/login/register requires it.
- Users have unique usernames; search them via `GET /api/users?q=`.
- Groups are scoped: you only see groups you belong to. Join via invite code,
  admins add members by username (`POST /api/groups/{id}/members`), invite
  codes rotate, acting identity always derives from the token (no impersonation).
- Admins own tables/rules/flows/schedules/agents; members chat, submit, accept,
  run agents and read every shared dashboard.

Config: AI is **Gemini only** via Google's OpenAI-compatible endpoint. Set
`PLATFORM_AI_KEY` to a Gemini API key (Google AI Studio, free tier). Free
models per feature: rule compilation + agent brains → `gemini-3.8-flash`;
view planning + chat hints → `gemini-3.5-flash-lite`. Override with
`PLATFORM_AI_MODEL` (all) or `PLATFORM_AI_MODEL_COMPILE/_BRAIN/_PLAN/_SUGGEST`.
With no key the app uses the deterministic local fallback — the Rules badge
and `/api/ai-status` show which is active.

## What lives where

- `app/main.py` — all REST endpoints + serves `/`
- `app/models.py` — single-hub schema (memberships hub, ≤5 tables/group)
- `app/rules.py` — NL → deterministic checks + evaluator
- `app/ai.py` — LLM compilation / view planning / agent brains / web search
- `app/agents_logic.py` — Query → Act → Return → Mutate
- `app/workflows.py` — redirects, inbox routing, generated views
- `app/scheduler_loop.py` — group cron jobs (15s tick, same process)
- `app/seed.py` — Driver Network + Campus demos

## Demo every feature (5 minutes)

1. **Chat** — send a message; try `/query verified drivers`, `/task Night patrol`,
   `/form Package intake`, `/agent find matches`, `/schedule daily 09:00 report`.
2. **Data** — switch tables (Members/Vehicles/Dispatches/Documents/Payments).
   Creating a 6th table is rejected (five-table cap).
3. **Tasks** — open Tasks → Offers → *Accept as me*. Ineligible members are
   blocked live (e.g. rating/stake rules). Responding fires flows.
4. **Forms** — Fill *List a package* with value `50000` as low-stake Emeka Obi →
   rejected (`package value ≤ stake`). Same form as Samuel Adeyemi → accepted,
   writes a Dispatch record + classifies the member.
5. **Rules** — Rules tab → type NL → *Preview compilation* (AI or local fallback)
   → *Save as draft* → *Confirm* → *Test on me*. Only confirmed JSON enforces.
6. **Schedules (the cron jobs)** — Schedules tab shows 07:00 inspect, 12:00 match,
   17:00 follow-up, weekly report. *Run now* any of them; check *Runs* log.
   Create your own: daily/weekly/interval/cron + inspect|match|follow_up|report|
   push_inbox|create_task|run_agent. Every firing also emits `schedule.fired`
   so Flows can chain off time itself.
7. **Flows** — accept the Airport task → member gets *Next: QR handoff* inbox
   (redirect). Confirm a browser QR event → public receipt (redirect).
   Toggle flows on/off; add `form.submitted → run_agent` style chains.
8. **Agents** — Agents tab → Run *Compliance search* → proposal in inbox →
   *Confirm & mutate* writes `verification=Verified` after rule validation.
   *Job-finding agent* blends profile match + live web search into the inbox.
   *Web search* agent does real external lookup.
9. **Views (shared dashboards)** — every member sees the same AI-wired
   dashboards: stat cards + *Open dashboard* row tables,
   or *Generate with AI*: `verified drivers in Lagos`.
10. **Inbox** — sidebar Inbox + main Inbox tab; filter all/personal/public.
    Task offers, agent proposals, reminders and reports route here, not broadcast.
11. **Browser** — Browser tab → *Open external action* (payment/booking/QR) →
    *Confirm ✓* → status writes back + fires `status.confirmed` flows.

Seeded story: Airport Dispatch Shift is offered only to 3 eligible drivers
(rule + tags); accepting chains to QR handoff; QR confirm chains to receipt —
tables → tasks → schedules → agents → flows, all without leaving the chat.

## Chat powers

- **Replies** — hover any message → ↩, quote bar above the composer, click a
  quote to jump to the original (with flash highlight).
- **Reactions** — limited to the coordination set ✅ 👀 ⏳ 🙏 ⚠️ ❤️, one tap
  toggles, counts shown under each message.
- **Forwarding** — ➦ sends a copy (labeled with its origin) to any of your
  other groups.
- **Emoji picker** — ☺ in the composer for desktop.
- **In-chat cards** — the + menu posts real tasks (*I'm available* accepts
  inline) and forms (*Start form* fills inline); agents post confirmable
  proposal cards; **Share to chat** on any View drops a live dashboard portal.

## Validation where you need it

No separate Rules tab. Task and form builders carry a rule dropdown (reuse any
existing rule) plus a plain-words box (admins: compiled, confirmed and
attached in one motion).

Engaged state is sticky: accepted tasks show Accepted ✓, declined stay
declined, submitted forms show Submitted ✓ — double taps are idempotent
server-side too. Forms can allow multiple responses if the builder says so.

## Draft with AI (Gemini)

Every builder has an ✨ AI draft button (tasks, forms, schedules, flows,
tables — manual creation stays). Prompt → structured spec preview (with the
model named, or local-fallback badge) → confirm → created and announced in
chat. Specs are validated strictly on apply: unknown actions, triggers,
types, cross-group ids and the five-table cap are all rejected.

## Paced catch-up (phased messages)

On by default in every group: new messages arrive one at a time, gap
settable up to 5s, with Pause and Skip-to-latest on the floating control.
Opt out per group in Group Info → *Paced catch-up*.
Try it in **Campus Clothes Exchange** (invite `9C057F`), seeded with a 27-message
flood and paced ON for the owner. A 2-minute auto-pilot **Pulse** schedule
lives in Crowd Lagos with an ON/OFF banner in Schedules.

## Files, sharing, credit

- Paperclip button uploads (10 MB max): images render inline in chat, other
  files as download cards — never a bare path.
- Any task or form, made in chat or in its tab, can be posted to chat: + menu →
  *Post an existing task/form*, Share buttons on every card.
- Every task/form card names the member who initiated it — Platform only speaks
  for genuinely automated output (schedules, status reports).

## Hackathon Crew

`ola`, `taiwo`, `ife` + you. Check-in task: only Ola and you tapped yes.
The **Role assigner** agent reads the check-ins plus members' saved details and
inboxes each attendee a fitting role (Backend engineer, Team captain) — mirrored
on the Team board. A daily **Morning spark** schedule keeps the crew motivated.
