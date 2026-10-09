"""Deterministic rule compiler + evaluator.

compile-once philosophy: NL text is compiled a single time into a stored
JSON check-list, shown back to the creator, and only the stored JSON runs
afterwards. The LLM proposes; this module enforces.
"""
import re


def _num(s):
    try:
        return float(s)
    except Exception:
        return None


def local_compile(nl):
    """Heuristic NL -> checks. Deterministic, no network."""
    t = (nl or "").lower()
    checks, notes = [], []

    # cross-field: package value vs courier stake
    if "stake" in t and ("value" in t or "price" in t or "package" in t) and any(
        k in t for k in ["<=", "≤", "never exceed", "cannot exceed", "must not exceed", "not exceed", "exceed"]
    ):
        checks.append({
            "field": "value", "op": "<=", "value": "$profile.stake_balance",
            "source": "cross", "description": "package value must be ≤ courier's available stake",
        })
        notes.append("package value ≤ courier's available stake")

    # rating thresholds
    m = re.search(r"rating\s*(above|over|>=?|at least|minimum)?\s*(\d(?:\.\d+)?)", t)
    if m or "4.7" in t or "rating" in t:
        val = float(m.group(2)) if m else 4.7
        op = ">=" if ("at least" in t or "minimum" in t or ">=" in t) else ">"
        if "above" in t and m:
            op = ">"
        checks.append({"field": "rating", "op": op, "value": val, "source": "profile",
                       "description": f"rating {op} {val}"})
        notes.append(f"rating {op} {val}")

    # verification / active
    if any(k in t for k in ["verified", "active member", "verification"]):
        if "unverified" not in t:
            checks.append({"field": "verification", "op": "in", "value": ["Verified", "active"],
                           "source": "profile", "description": "member must be verified/active"})
            notes.append("verified/active only")

    # availability
    if "availab" in t:
        checks.append({"field": "availability", "op": "in",
                       "value": ["Available", "available"],
                       "source": "profile", "description": "member must be available"})
        notes.append("available only")

    # city / location
    m2 = re.search(r"(?:in|near|from|city:?)\s+([a-z]+)", t)
    for city in ["lagos", "ikeja", "lekki", "yaba", "abuja"]:
        if city in t:
            checks.append({"field": "city", "op": "==", "value": city.title(),
                           "source": "profile", "description": f"city must be {city.title()}"})
            notes.append(f"city = {city.title()}")
            break

    # transport mode
    for mode in ["bike", "motorcycle", "car", "on foot", "foot"]:
        if mode in t:
            checks.append({"field": "transport_mode", "op": "==", "value": mode,
                           "source": "profile", "description": f"transport mode must be {mode}"})
            notes.append(f"mode = {mode}")
            break

    # skills
    for skill in ["php", "sql", "python", "driving", "laravel"]:
        if skill in t:
            checks.append({"field": "skills", "op": "contains", "value": skill,
                           "source": "profile", "description": f"skills must include {skill}"})
            notes.append(f"skill: {skill}")

    # only admins
    if "only admin" in t or "admins only" in t:
        checks.append({"field": "role", "op": "==", "value": "admin",
                       "source": "profile", "description": "admins only"})
        notes.append("admins only")

    # required submission field present
    m3 = re.search(r"(license|document|receipt|price|value)[\w\s]*required", t)
    if m3:
        checks.append({"field": m3.group(1), "op": "exists", "value": True,
                       "source": "submission", "description": f"{m3.group(1)} is required"})
        notes.append(f"{m3.group(1)} required")

    needs_review = len(checks) == 0
    if needs_review:
        notes.append("No concrete constraint detected — needs creator review")
    explanation = "IF " + (" AND ".join("[" + n + "]" for n in notes) if notes else nl.strip())
    return {"checks": checks, "needs_review": needs_review}, explanation


def _get(obj, field, default=None):
    if isinstance(obj, dict):
        for k in (field, field.lower(), field.replace("_", " "), field.title()):
            if k in obj:
                return obj[k]
        # case-insensitive scan
        fl = field.lower()
        for k, v in obj.items():
            if str(k).lower() == fl:
                return v
    return default


def _compare(actual, op, expected):
    try:
        if op == "==":
            if isinstance(actual, str) and isinstance(expected, str):
                return actual.strip().lower() == expected.strip().lower()
            return actual == expected
        if op == "!=":
            return actual != expected
        if op in ("in",):
            if isinstance(expected, list):
                if isinstance(actual, str):
                    return actual.strip().lower() in [str(x).lower() for x in expected]
                return actual in expected
            return False
        if op == "contains":
            if actual is None:
                return False
            if isinstance(actual, list):
                return str(expected).lower() in [str(x).lower() for x in actual]
            return str(expected).lower() in str(actual).lower()
        if op == "exists":
            return actual is not None and actual != ""
        for sym in (">", ">=", "<", "<="):
            if op == sym:
                a, e = _num(actual), _num(expected)
                if a is None or e is None:
                    return False
                if sym == ">":
                    return a > e
                if sym == ">=":
                    return a >= e
                if sym == "<":
                    return a < e
                return a <= e
    except Exception:
        return False
    return False


def evaluate(compiled, profile=None, submission=None):
    """Run stored checks. Returns (passed: bool, reasons: list[str])."""
    profile = profile or {}
    submission = submission or {}
    checks = (compiled or {}).get("checks", []) if isinstance(compiled, dict) else []
    reasons = []
    ok = True
    for c in checks:
        field, op = c.get("field", ""), c.get("op", "==")
        src = c.get("source", "profile")
        raw_expected = c.get("value")
        # resolve $profile.xxx references for cross checks
        expected = raw_expected
        if isinstance(raw_expected, str) and raw_expected.startswith("$profile."):
            expected = _get(profile, raw_expected.split(".", 1)[1])
        if src == "profile":
            actual = _get(profile, field)
        elif src == "submission":
            actual = _get(submission, field, _get(profile, field))
        else:  # cross: actual from submission, expected from profile
            actual = _get(submission, field, _get(submission, "value", _get(submission, "price")))
        passed = _compare(actual, op, expected)
        if not passed:
            ok = False
            reasons.append(f"Failed: {c.get('description', field + ' ' + op)} (had {actual!r}, needed {op} {expected!r})")
    return ok, reasons


def member_matches_filter(profile, tags, filt):
    """Audience/filter matching for tasks, views, schedules."""
    tags = tags or []
    if not filt:
        return True
    if isinstance(filt, dict) and "and" in filt:
        return all(member_matches_filter(profile, tags, f) for f in filt["and"])
    field, op, value = filt.get("field"), filt.get("op", "=="), filt.get("value")
    if field in ("tag", "tags"):
        return _compare(tags, "contains" if op == "==" else op, value)
    return _compare(_get(profile, field or ""), op, value)
