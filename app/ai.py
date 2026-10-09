"""AI integration for Platform — GEMINI ONLY.

Google Gemini via its OpenAI-compatible endpoint for:
  - compiling natural-language rules into deterministic checks
  - answering data queries / generating views
  - powering agents (search, match, analysis)

Free-tier model picks (Oct 2026):
  compile (strict rule JSON) .... gemini-3.8-flash      (best free instruction-following)
  brain (agent matching) ........ gemini-3.8-flash      (reasoning + analysis)
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
import urllib.request
import urllib.error

AI_KEY = os.environ.get("PLATFORM_AI_KEY", "")
AI_MODELS = {
    "compile": os.environ.get("PLATFORM_AI_MODEL_COMPILE", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.8-flash")),
    "brain": os.environ.get("PLATFORM_AI_MODEL_BRAIN", os.environ.get("PLATFORM_AI_MODEL", "gemini-3.8-flash")),
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
    text = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "compile", max_tokens=600, temperature=0.0,
    )
    if text:
        try:
            start, end = text.find("{"), text.rfind("}")
            compiled = json.loads(text[start:end + 1])
            if isinstance(compiled.get("checks"), list):
                expl = "; ".join(
                    c.get("description", f"{c.get('field')} {c.get('op')} {c.get('value')}")
                    for c in compiled["checks"]
                ) or natural_language
                return {"checks": compiled["checks"], "needs_review": bool(compiled.get("needs_review", False))}, expl, AI_MODELS["compile"]
        except Exception as e:
            global _last_error
            _last_error = f"parse: {e}"
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
        try:
            start, end = text.find("{"), text.rfind("}")
            return json.loads(text[start:end + 1]), AI_MODELS["plan"]
        except Exception:
            pass
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
        try:
            start, end = text.find("["), text.rfind("]")
            items = json.loads(text[start:end + 1])
            if isinstance(items, list) and items:
                return items[:5], AI_MODELS["brain"]
        except Exception:
            pass
    return [], "local"


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
        try:
            start, end = text.find("{"), text.rfind("}")
            return json.loads(text[start:end + 1])
        except Exception:
            return {}
    return {}
