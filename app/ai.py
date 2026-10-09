"""AI integration for Platform — GEMINI ONLY.

Google Gemini via its OpenAI-compatible endpoint for:
  - compiling natural-language rules into deterministic checks
  - answering data queries / generating views
  - powering agents (search, match, analysis)

Free-tier model picks (Oct 2026):
  compile (strict rule JSON) .... gemini-3.6-flash      (best free instruction-following)
  brain (agent matching) ........ gemini-3.6-flash      (reasoning + analysis)
  plan (view filter mapping) .... gemini-3.5-flash-lite (fastest, cheapest)
  suggest (chat hints) .......... gemini-3.5-flash-lite (tiny, high-frequency)

Design: compile-once. Gemini proposes a structured rule; we store it,
show it back to the creator, and enforce ONLY the stored JSON afterwards.
Every call has a deterministic local fallback so the demo works offline
or when the key/network is unavailable.

Env:
  PLATFORM_AI_KEY            (Gemini API key from Google AI Studio, free tier)
  PLATFORM_AI_BASE_URL       (default: Google's OpenAI-compatible endpoint)
  PLATFORM_AI_MODEL          (overrides every role below)
  PLATFORM_AI_MODEL_COMPILE / _BRAIN / _PLAN / _SUGGEST
"""
import json
import os
import re
import urllib.request
import urllib.error

AI_KEY = os.environ.get("PLATFORM_AI_KEY", "")
AI_MODELS = {
    "compile": os.environ.get("PLATFORM_AI_MODEL_COMPILE", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.5-flash-lite")),
    "brain": os.environ.get("PLATFORM_AI_MODEL_BRAIN", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.6-flash")),
    "plan": os.environ.get("PLATFORM_AI_MODEL_PLAN", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.5-flash-lite")),
    "suggest": os.environ.get("PLATFORM_AI_MODEL_SUGGEST", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.5-flash-lite")),
}
BASE_CANDIDATES = [
    s.strip() for s in os.environ.get(
        "PLATFORM_AI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ).split(",") if s.strip()
]
AI_MODEL = AI_MODELS["compile"]

_last_error = ""


def ai_status():
    return {
        "provider": "gemini",
        "configured": bool(AI_KEY),
        "key_prefix": (AI_KEY[:5] + "..." + AI_KEY[-4:]) if AI_KEY else "",
        "models": AI_MODELS,
        "model": AI_MODEL,
        "bases": BASE_CANDIDATES,
        "last_error": _last_error,
    }


def _chat(messages, role="compile", max_tokens=800, temperature=0.1, timeout=25):
    """Gemini chat via its OpenAI-compatible endpoint. Returns text or None."""
    global _last_error
    model = AI_MODELS.get(role, AI_MODEL)
    if not AI_KEY:
        _last_error = "no Gemini key configured (PLATFORM_AI_KEY)"
        return None
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    for base in BASE_CANDIDATES:
        url = base.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {AI_KEY}",
                "HTTP-Referer": "https://platform.local",
                "X-Title": "Platform Lobby",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
                choices = payload.get("choices", [])
                if choices:
                    msg = choices[0].get("message", {})
                    text = msg.get("content", "")
                    if text:
                        return text
        except Exception as e:  # try next base
            _last_error = f"{base}: {e}"
            continue
    return None


ALLOWED_OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "contains", "exists"}
ALLOWED_SOURCES = {"profile", "submission", "cross"}


def _strip_fences(text):
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()
    return t


def _balanced_objects(text):
    """Yield balanced {...} substrings (outermost first)."""
    objs = []
    depth = 0
    start = -1
    instr = False
    esc = False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(text[start:i + 1])
                    start = -1
    return objs


def extract_json(text, want_key=None):
    """Find the first JSON object (or array) that parses and has want_key."""
    t = _strip_fences(text)
    cands = _balanced_objects(t)
    # also try arrays for brain responses
    if want_key == "[":
        depth = 0
        start = -1
        for i, ch in enumerate(t):
            if ch == "[":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "]":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        cands.append(t[start:i + 1])
        for c in cands:
            try:
                v = json.loads(c)
                if isinstance(v, list):
                    return v
            except Exception:
                continue
        return None
    for c in cands:
        try:
            v = json.loads(c)
        except Exception:
            continue
        if isinstance(v, dict) and (want_key is None or want_key in v):
            return v
    return None


def clean_checks(raw):
    """Strict-shape the checks list; returns (checks, dropped)."""
    checks, dropped = [], 0
    if not isinstance(raw, list):
        return [], 1
    for c in raw:
        if not isinstance(c, dict):
            dropped += 1
            continue
        field, op, src = c.get("field"), c.get("op"), c.get("source", "profile")
        if not isinstance(field, str) or not field.strip():
            dropped += 1
            continue
        if op not in ALLOWED_OPS:
            dropped += 1
            continue
        if src not in ALLOWED_SOURCES:
            src = "profile"
        val = c.get("value")
        fld = field.strip().lower()
        if fld == "verification" and val is True:
            op, val = "in", ["Verified", "active"]
        if fld in ("rating", "value", "stake_balance") and isinstance(val, str):
            try:
                val = float(val)
            except ValueError:
                pass
        checks.append({"field": field.strip()[:60], "op": op, "value": val,
                       "source": src,
                       "description": str(c.get("description") or f"{field} {op} {val}")[:160]})
    return checks, dropped


def ai_compile_rule(natural_language, schema_context=""):
    """Ask the LLM to compile NL -> deterministic checks JSON.

    Returns (compiled_dict, explanation, model_used). Falls back to local parser.
    """
    from app.rules import local_compile
    system = (
        "You compile group validation rules into deterministic JSON. "
        "Respond with ONLY a JSON object, no markdown. Schema: "
        '{"checks":[{"field":string,"op":string,"value":any,"source":"profile|submission|cross","description":string}],'
        '"needs_review":bool}. '
        "Allowed ops: ==,!=,>,>=,<,<=,in,contains,exists. "
        "Profile fields: verification, rating, availability, city, skills, stake_balance, transport_mode, role, tags. "
        "Submission fields depend on the form (e.g. value, license_number). "
        "Cross checks compare submission vs profile, e.g. {field:'value',op:'<=',value:'$profile.stake_balance',source:'cross'}. "
        "Set needs_review=true if the text is ambiguous."
    )
    user = f"Rule: {natural_language}\nSchema context: {schema_context[:1500]}"
    for attempt in ("Return ONLY the JSON object, no other text.",
                    "Your last reply was not valid JSON. Reply with NOTHING but the JSON object."):
        text = _chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user + "\n" + attempt}],
            "compile", max_tokens=600, temperature=0.0,
        )
        if not text:
            break
        compiled = extract_json(text, "checks")
        if compiled:
            checks, dropped = clean_checks(compiled.get("checks"))
            if checks:
                expl = "; ".join(c["description"] for c in checks)
                if dropped:
                    expl += f" ({dropped} invalid check(s) dropped)"
                return ({"checks": checks,
                         "needs_review": bool(compiled.get("needs_review", False)) or dropped > 0},
                        expl, AI_MODELS["compile"])
    global _last_error
    _last_error = "gemini returned no valid checks; used local fallback"
    compiled, expl = local_compile(natural_language)
    return compiled, expl + " (local fallback)", "local-deterministic"


def ai_plan_view(natural_language, tables_snapshot):
    """Turn 'show me verified drivers in Lagos' into {table_key, filter, columns}."""
    system = (
        "You map a natural-language view request to a structured query. "
        "Respond with ONLY JSON: {\"table_key\":string,\"filter\":{\"field\":string,\"op\":string,\"value\":any},"
        "\"columns\":[string],\"template\":string}. Keep it simple."
    )
    text = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Request: {natural_language}\nTables: {tables_snapshot[:1800]}"}],
        "plan", max_tokens=400, temperature=0.0,
    )
    if text:
        planned = extract_json(text, "filter")
        if planned:
            return planned, AI_MODELS["plan"]
    return None, "local"


def ai_agent_brain(kind, profile, context=""):
    """Give an agent a brain: propose structured result_items for a member.

    Tries the LLM; falls back to deterministic matchers in agents_logic.
    Returns (items_list, model_used).
    """
    system = (
        "You are a group-coordination agent. Given a member profile and context, "
        "propose 2-4 structured matches. Respond with ONLY a JSON array of objects "
        "with keys: title, detail, score (0-100), action. No markdown."
    )
    text = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Agent kind: {kind}\nProfile: {json.dumps(profile)[:1200]}\nContext: {context[:1200]}"}],
        "brain", max_tokens=700, temperature=0.4,
    )
    if text:
        items = extract_json(text, "[")
        if isinstance(items, list):
            items = [i for i in items
                     if isinstance(i, dict) and isinstance(i.get("title"), str)][:5]
            if items:
                return items, AI_MODELS["brain"]
    return [], "local"


def ai_ask_plan(question, tables_spec):
    """Turn 'total value of open packages' into {table_key, op, column, filter}.

    tables_spec: [{key, columns: [{name, type, options}]}].
    Tries Gemini, validates strictly, else deterministic local parse.
    Returns (plan_dict|None, model_used).
    """
    system = (
        "You turn a question about table records into a computation plan. "
        'Respond with ONLY JSON: {"table_key":string (which table),'
        '"op":one of count|sum|avg|min|max|list,'
        '"column":string (numeric column for sum|avg|min|max, else empty),'
        '"filter":object like {"status":"open"} or {} (equality filters only)}. '
        "Use exact table keys and column names from the context."
    )
    text = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": "Tables: %s\nQuestion: %s" % (
             json.dumps(tables_spec)[:1500], (question or "")[:400])}],
        "plan", max_tokens=300, temperature=0.0,
    )
    if text:
        plan = extract_json(text, "op")
        if isinstance(plan, dict) and _valid_ask_plan(plan, tables_spec):
            return plan, AI_MODELS["plan"]
    return _local_ask_plan(question, tables_spec), "local-deterministic"


def _valid_ask_plan(plan, tables_spec):
    if not isinstance(plan, dict):
        return False
    keys = {t["key"] for t in tables_spec}
    if plan.get("table_key") not in keys:
        return False
    if plan.get("op") not in ("count", "sum", "avg", "min", "max", "list"):
        return False
    cols = {c["name"] for t in tables_spec if t["key"] == plan.get("table_key") for c in t.get("columns", [])}
    if plan.get("op") in ("sum", "avg", "min", "max") and plan.get("column") not in cols:
        return False
    filt = plan.get("filter") or {}
    if not isinstance(filt, dict):
        return False
    return all(k in cols and not isinstance(v, (dict, list)) for k, v in filt.items())


def _local_ask_plan(question, tables_spec):
    q = (question or "").lower()
    table = tables_spec[0]["key"] if tables_spec else ""
    for t in tables_spec:
        stem = t["key"].split("_")[0]
        if stem and stem in q:
            table = t["key"]
            break
    cols = [c for t in tables_spec if t["key"] == table for c in t.get("columns", [])]
    numeric = [c["name"] for c in cols if c.get("type") == "number"]
    money = [n for n in numeric if re.search(r"(amount|value|price|fee|cost|total|balance|stake)", n, re.I)]
    op, col = "list", ""
    if re.search(r"\b(total|sum|add up|combined)\b", q) and (money or numeric):
        op, col = "sum", (money or numeric)[0]
    elif re.search(r"\b(averag|avg|mean)\b", q) and (money or numeric):
        op, col = "avg", (money or numeric)[0]
    elif re.search(r"\bhow many|count|number of\b", q):
        op = "count"
    elif re.search(r"\b(cheapest|lowest|min|minimum)\b", q) and (money or numeric):
        op, col = "min", (money or numeric)[0]
    elif re.search(r"\b(most|highest|largest|biggest|expensive|max)\b", q) and (money or numeric):
        op, col = "max", (money or numeric)[0]
    filt = {}
    for c in cols:
        for opt in (c.get("options") or []):
            if isinstance(opt, str) and opt and opt.lower() in q:
                filt[c["name"]] = opt
                break
    return {"table_key": table, "op": op, "column": col, "filter": filt}


def web_search(query, count=5):
    """Lightweight external search for agents (DuckDuckGo instant + html fallback).

    No key required. Returns [{title, detail, url}]. Never raises.
    """
    import html as _html
    out = []
    try:
        q = urllib.parse.quote(query) if hasattr(urllib, "parse") else query
    except Exception:
        q = query
    try:
        import urllib.parse as _up
        url = "https://api.duckduckgo.com/?q=" + _up.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Platform/1.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            payload = json.loads(r.read().decode())
            for t in (payload.get("RelatedTopics") or [])[:count]:
                if isinstance(t, dict) and t.get("Text"):
                    out.append({"title": t.get("Text", "")[:90], "detail": t.get("Text", "")[:220], "url": t.get("FirstURL", "")})
            abstract = payload.get("AbstractText") or ""
            if abstract and len(out) < count:
                out.insert(0, {"title": (payload.get("Heading") or query)[:90], "detail": abstract[:220], "url": payload.get("AbstractURL", "")})
    except Exception:
        pass
    if not out:
        # deterministic placeholder so the agent demo still shows reach
        out = [{"title": f"External result for '{query}'", "detail": "External lookup unavailable offline — structured placeholder.", "url": ""}]
    return out[:count]


def ai_chat_structured_suggest(message_text, schema_snapshot=""):
    """Chat helper: suggest which table/rule a free-text message maps to."""
    system = (
        "You turn a chat message into a structured suggestion. Respond with ONLY JSON: "
        '{"table_key":string,"fields":object,"why":string}. Use empty fields if nothing maps.'
    )
    text = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Message: {message_text[:600]}\nSchema: {schema_snapshot[:1200]}"}],
        "suggest", max_tokens=300, temperature=0.0,
    )
    if text:
        return extract_json(text, "table_key") or {}
    return {}


GENERATE_SCHEMAS = {
    "task": "Respond with ONLY JSON: {\"title\":string(required,<=120 chars),\"description\":string,\"payout_text\":string,\"audience_tags\":[string role words like courier|sender|buyer],\"rule_name\":string(the one existing rule that should govern this, from context, or empty)}. Derive a SHORT title; never copy the whole request as the title.",
    "form": "Respond with ONLY JSON: {\"title\":string(required,<=120 chars),\"description\":string,\"table_key\":string(which existing table key this writes to, from context, or empty for none), fields:[{\"name\":string snake_case,\"label\":string,\"type\":one of text|number|select,\"required\":bool,\"options\":[string] for select}],\"allow_multiple\":bool}. Turn each requested question into a field; max 8 fields.",
    "schedule": "Respond with ONLY JSON: {\"name\":string(required),\"kind\":one of once|interval|daily|weekly|cron,\"time_of_day\":string HH:MM for daily|weekly,\"interval_seconds\":int for interval,\"cron_expr\":string for cron,\"action\":one of inspect|match|follow_up|report|motivate|push_inbox|create_task|run_agent,\"note\":string}. Default kind daily 09:00 action report unless the request says otherwise.",
    "redirect": "Respond with ONLY JSON: {\"name\":string(required),\"trigger_event\":one of offer.accepted|task.accepted|task.completed|form.submitted|status.confirmed|schedule.fired|member.joined,\"action\":one of push_inbox|push_public|create_task|write_record|run_agent,\"message\":string(the text of the next step)}. Map accept/complete/submit/confirm/time/join words to the closest trigger.",
    "table": "Respond with ONLY JSON: {\"name\":string(required, a short plural noun like Shipments, max 60 chars -- NEVER the whole request),\"description\":string,\"columns\":[{\"name\":string snake_case,\"label\":string,\"type\":one of text|number|select}(max 8 columns)].",
}

GENERATE_ROLES = {"task": "compile", "form": "compile", "schedule": "plan", "redirect": "plan", "table": "plan"}


def _slug(s, maxlen=40):
    s = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return (s or "field")[:maxlen]


def local_propose(kind, prompt, ctx=None):
    ctx = ctx or {}
    t = (prompt or "").strip()
    low = t.lower()
    tables = ctx.get("tables") or []
    rules = ctx.get("rules") or []
    if kind == "task":
        pay = ""
        m = re.search(r"(?:\$|₦)\s?(\d[\d,]*)", t) or re.search(r"(?:pays?|payout|fee)\s+(?:\$|₦)?\s?(\d[\d,]*)", t, re.I)
        if m:
            pay = "$" + m.group(1)
        title = re.split(r"[,;\n]", t, 1)[0]
        title = re.sub(r"\b(pays?|payout|fee)\b.*$", "", title, flags=re.I)
        title = re.sub(r"\b(only|for)\b.*$", "", title, flags=re.I)
        title = re.sub(r"^(task|create|add|post|make)\s+", "", title, flags=re.I).strip()[:120] or "Untitled task"
        tags = [w for w in ("courier", "sender", "buyer", "rider", "driver", "admin") if w in low]
        rule_name = ""
        for rn in rules:
            if any(k in low for k in rn.lower().split()[:3] if len(k) > 3):
                rule_name = rn
                break
        return {"title": title, "description": t[:300], "payout_text": pay, "audience_tags": tags, "rule_name": rule_name}, []
    if kind == "form":
        parts = [p.strip() for p in re.split(r"[,;\n]+", t) if p.strip()]
        title = parts[0][:120] if parts else "Untitled form"
        rest = parts[1:]
        if parts and ":" in parts[0]:
            head, tail = parts[0].split(":", 1)
            if head.strip():
                title = head.strip()[:120]
            if tail.strip():
                rest = [tail.strip()] + rest
        qs = [re.split(r"\.\s+(?=[A-Z])", q)[0].strip() for q in rest[:7]] or ["Answer"]
        qs = [q for q in qs if q] or ["Answer"]
        tk = ""
        for key in tables:
            stem = key.split("_")[0]
            if stem and stem in low:
                tk = key
                break
        return {"title": title, "description": "", "table_key": tk,
                "fields": [{"name": _slug(q), "label": q[:60], "type": "number" if re.search(r"(amount|qty|price|value|count|number|rating|votes|age|fee|cost|total)", q, re.I) else "text", "required": True} for q in qs],
                "allow_multiple": False}, []
    if kind == "schedule":
        spec = {"name": t[:80] or "Scheduled job", "kind": "daily", "time_of_day": "09:00", "action": "report", "note": t[:200]}
        m = re.search(r"(\d{1,2}):(\d{2})", t)
        if m:
            spec["time_of_day"] = "%02d:%s" % (int(m.group(1)), m.group(2))
        m2 = re.search(r"every\s+(\d+)\s*(min|sec|hour)", low)
        if m2:
            spec["kind"] = "interval"
            spec["interval_seconds"] = int(m2.group(1)) * (60 if "min" in m2.group(2) else 3600 if "hour" in m2.group(2) else 1)
            spec.pop("time_of_day", None)
        for act in ("match", "inspect", "follow_up", "report", "motivate"):
            if act.replace("_", " ") in low or act in low:
                spec["action"] = act
                break
        return spec, []
    if kind == "redirect":
        trig = "task.completed"
        for key, ev in (("accept", "offer.accepted"), ("complet", "task.completed"), ("submit", "form.submitted"), ("confirm", "status.confirmed"), ("schedul", "schedule.fired"), ("join", "member.joined")):
            if key in low:
                trig = ev
                break
        return {"name": t[:80] or "Untitled flow", "trigger_event": trig, "action": "push_inbox", "message": t[:300]}, []
    if kind == "table":
        parts = [p.strip() for p in re.split(r"[,;\n]+", t) if p.strip()]
        raw = parts[0] if parts else "Untitled table"
        raw = re.sub(r"(?i)^(new|create|add|make|track|tracking|table|a|the)\s+", "", raw).strip()
        name_part, _, col_part = raw.partition(" with ")
        if not col_part:
            name_part, _, col_part = raw.partition(" including ")
        raw = re.split(r"[:\-–—]", name_part)[0].strip()
        words = [w for w in re.sub(r"[^a-zA-Z0-9 ]", "", raw).split() if w.lower() not in ("with", "and", "for", "table", "track", "list", "record")]
        name = " ".join(w.capitalize() for w in words[:3]) or "Untitled table"
        name = name[:60]
        rest = [p.strip() for seg in ((col_part + "," + ",".join(parts[1:])) if col_part else ",".join(parts[1:])).split(",") for p in re.split(r"\s+and\s+|;", seg) if p.strip()][:8]
        cols = [{"name": _slug(c), "label": c[:40],
                 "type": "number" if re.search(r"(amount|qty|price|value|count|number|rating|votes|age|fee|cost|total|balance|stake)", c, re.I) else "text"}
                for c in rest]
        return {"name": name, "description": "",
                "columns": cols or [{"name": "title", "label": "Title", "type": "text"}]}, []
    return {}, ["unknown kind"]


def ai_propose(kind, prompt, snapshot="", ctx=None):
    if kind not in GENERATE_SCHEMAS:
        return {}, "local", ["unknown kind: %s" % kind]
    if not (prompt or "").strip():
        return {}, "local", ["describe what you want first"]
    system = ("You turn a group organizer's request into a structured creation spec. " + GENERATE_SCHEMAS[kind])
    text = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": "Request: %s\nContext: %s" % (prompt[:800], snapshot[:1200])}],
        GENERATE_ROLES[kind], max_tokens=600, temperature=0.2,
    )
    if text:
        spec = extract_json(text)
        if isinstance(spec, dict) and spec:
            return spec, AI_MODELS[GENERATE_ROLES[kind]], []
        return local_propose(kind, prompt, ctx)[0], "local-fallback", ["gemini returned no usable spec"]
    spec, _ = local_propose(kind, prompt, ctx)
    return spec, "local-deterministic", []
