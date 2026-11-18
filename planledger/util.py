"""Shared helpers: hashing, number/date/time parsing, entity matching, formatting.

Nothing here knows about any specific event, sender, or price. Everything is
generic text handling that the reader, reconciler, planner and validator share.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- numbers --

_MULT = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lac": 100_000,
         "m": 1_000_000, "million": 1_000_000}


def to_int(v, default=None):
    """Coerce model output such as 15000, "15,000", "Rs 15,000", "50k" to int."""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v)) if math.isfinite(v) and abs(v - round(v)) < 1e-9 else default
    if isinstance(v, str):
        s = v.strip().lower()
        s = re.sub(r"\b(rs\.?|pkr|rupees?|per|each)\b", " ", s)
        m = re.fullmatch(r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([a-z]*)\s*", s)
        if not m:
            return default
        num = float(m.group(1).replace(",", ""))
        suf = m.group(2)
        if suf:
            if suf not in _MULT:
                return default
            num *= _MULT[suf]
        return int(round(num)) if abs(num - round(num)) < 1e-9 else default
    return default


def pkr(n) -> str:
    if n is None:
        return "unknown"
    return f"Rs {int(n):,}"


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# ------------------------------------------------------------------ dates --

_DATE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_SLASH = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
_DATE_DM = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?(?:,?\s*(\d{4}))?")
_DATE_MD = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?")


def parse_date(text):
    """Return (year|None, month, day) or None. Day-first for numeric dates."""
    if not text:
        return None
    t = str(text)
    m = _DATE_ISO.search(t)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _DATE_SLASH.search(t)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return (y, int(m.group(2)), int(m.group(1)))
    m = _DATE_DM.search(t)
    if m and m.group(2)[:3].lower() in MONTHS:
        return (int(m.group(3)) if m.group(3) else None, MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    m = _DATE_MD.search(t)
    if m and m.group(1)[:3].lower() in MONTHS:
        return (int(m.group(3)) if m.group(3) else None, MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    return None


def date_cmp(a, b):
    """-1/0/1 comparing two parse_date() results; years compared only if both known."""
    if a is None or b is None:
        return None
    if a[0] is not None and b[0] is not None and a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    ka, kb = (a[1], a[2]), (b[1], b[2])
    return (ka > kb) - (ka < kb)


# ------------------------------------------------------------------ times --

_CLOCK = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?\s*(?:pkt|pst)?\s*$", re.I)


def parse_clock(s):
    """'13:30', '1:30 pm', '1pm' -> minutes since midnight, else None."""
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return None
    m = _CLOCK.match(str(s))
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = (m.group(3) or "").lower().replace(".", "")
    if ap:
        if not (1 <= h <= 12):
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 24 or mi > 59:
        return None
    return h * 60 + mi


def parse_offset(s):
    """'+45', 'T+45', '45 min' -> 45; else None."""
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, int):
        return s
    m = re.fullmatch(r"\s*(?:t\s*)?\+\s*(\d{1,4})\s*(?:min(?:ute)?s?)?\s*", str(s), re.I)
    return int(m.group(1)) if m else None


def fmt_clock(mins: int) -> str:
    mins %= 24 * 60
    return f"{mins // 60:02d}:{mins % 60:02d}"


def fmt_offset(mins: int) -> str:
    return f"T+{mins // 60}:{mins % 60:02d}"


# --------------------------------------------------------------- entities --

_STOP = {"the", "a", "an", "box", "boxes", "lunch", "meal", "meals", "wala", "wali", "wale",
         "ka", "ki", "ke", "of", "set", "per", "head", "item"}
_GENERIC = {"hall", "room", "bill", "cost", "costs", "fee", "fees", "expense", "expenses",
            "charge", "charges", "price", "payment", "rs", "bills"}


def _tokens(s: str):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return [t for t in s.split() if t]


def tokens(s):
    return _tokens(s)


def norm_entity(s) -> str:
    toks = _tokens(s)
    kept = [t for t in toks if t not in _STOP]
    return " ".join(kept or toks)


def _core(s) -> frozenset:
    toks = [t for t in _tokens(s) if t not in _STOP]
    core = [t for t in toks if t not in _GENERIC]
    return frozenset(core)


def same_entity(a, b) -> bool:
    """Same real-world thing? Token logic only (no fuzzy ratios: 'hall a' != 'hall b')."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    na, nb = norm_entity(a), norm_entity(b)
    if na == nb:
        return True
    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return False
    return ca == cb or ca < cb or cb < ca


# ------------------------------------------------------------- misc / text --

def short(s: str, n: int = 160) -> str:
    s = norm_ws(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def line_range(text: str, start: int, end: int):
    a = text.count("\n", 0, start) + 1
    b = text.count("\n", 0, max(start, end - 1)) + 1
    return [a, b]


def utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_now() -> str:
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y-%m-%d %H:%M:%S PKT")
    except Exception:  # tz database missing: say so rather than guess
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
