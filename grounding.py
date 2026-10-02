"""Grounding: decide in code whether an extracted value is supported by the page.

For every field the LLM returns {value, evidence}. A value is kept only if BOTH hold:
  1. quote check  - the evidence quote really occurs in the page text
                    (exact after normalisation, or rapidfuzz partial match >= threshold)
  2. value check  - the value is actually stated inside that quote
                    (dates parse to the same day, every number in the value appears in
                    the quote, names/terms overlap, URLs appear on the page)
Otherwise the field becomes "Not specified" with grounded = 0. Nothing is ever filled
from model memory, another site or a default.
"""
import re
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

import config

# ---------------------------------------------------------------- text normalisation
_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "−": "-", "\xa0": " ", "₹": " rs ", "|": " "})


def norm(s: str) -> str:
    s = (s or "").translate(_TRANS).lower()
    s = re.sub(r"\brs\s*\.", "rs ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- dates
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b"), "dmy"),
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r",?\s+(\d{4})\b", re.I), "d_mon_y"),
    (re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I), "mon_d_y"),
]


def find_dates(text: str) -> list[date]:
    """All calendar dates written in text. Indian convention: numeric dates are day-first."""
    out = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            try:
                if kind == "dmy":
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    y += 2000 if y < 100 else 0
                elif kind == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif kind == "d_mon_y":
                    d, mo, y = int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], int(m.group(3))
                else:
                    mo, d, y = _MONTHS[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3))
                out.append(date(y, mo, d))
            except (ValueError, KeyError):
                continue
    return out


def parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- numbers / money
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    """Numbers in text, with Indian 'lakh'/'crore' expanded, e.g. '2.5 lakh' -> {2.5, 250000}."""
    out = set()
    t = norm(text)
    for m in _NUM.finditer(t):
        raw = m.group(0).replace(",", "")
        try:
            n = float(raw)
        except ValueError:
            continue
        out.add(n)
        tail = t[m.end():m.end() + 8]
        if re.match(r"\s*(lakh|lakhs|lac|lacs)\b", tail):
            out.add(round(n * 100000, 2))
        elif re.match(r"\s*(crore|crores|cr)\b", tail):
            out.add(round(n * 10000000, 2))
    return out


def numbers_supported(value: str, quote: str) -> bool:
    """Every number in the value must appear in the quote (an invented number fails)."""
    v, q = numbers_in(value), numbers_in(quote)
    return all(any(abs(a - b) < 1e-6 for b in q) for a in v)


# ---------------------------------------------------------------- per-field value checks
_GENDER_TERMS = {
    "female": ("girl", "girls", "women", "woman", "female", "daughter", "lady"),
    "male": ("boy", "boys", "men", "male"),
    "any": ("all students", "boys and girls", "both", "any gender", "all genders"),
}
_STOP = set("the a an of and or for to in on by with is are be at as from any this that who which per".split())


def _content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", norm(s)) if w not in _STOP and len(w) > 1}


def value_supported(field: str, value: str, quote: str, page_links: list[tuple[str, str]] | None = None,
                    page_text: str = "") -> tuple[bool, str]:
    """Is `value` actually stated in `quote`? Returns (ok, reason)."""
    nq = norm(quote)
    if field in ("open_date", "close_date"):
        d = parse_iso(value)
        if not d:
            # non-date deadlines are only accepted when literally quoted (e.g. "open throughout the year")
            ok = norm(value) in nq
            return ok, "non-date value quoted verbatim" if ok else "value is not an ISO date and not in quote"
        ok = d in find_dates(quote)
        return ok, "date found in quote" if ok else "date not present in quote"

    if field == "application_url":
        u = norm(value).rstrip("/").replace("https://", "").replace("http://", "").removeprefix("www.")
        if u and u in norm(page_text).replace("https://", "").replace("http://", ""):
            return True, "URL written in page text"
        for href, _anchor in page_links or []:
            h = norm(href).rstrip("/").replace("https://", "").replace("http://", "").removeprefix("www.")
            if h == u:
                return True, "URL is a link on the page"
        return False, "URL not found on page"

    if not numbers_supported(value, quote):
        return False, "value contains a number not in the quote"

    if field == "gender":
        key = norm(value)
        for g, terms in _GENDER_TERMS.items():
            if g in key or any(t in key for t in terms):
                ok = any(re.search(rf"\b{re.escape(t)}\b", nq) for t in terms)
                return ok, f"gender term for '{g}' in quote" if ok else "gender not stated in quote"
        return False, "unrecognised gender value"

    if field == "name":
        score = fuzz.partial_ratio(norm(value), nq)
        return score >= 85, f"name match {score:.0f}"

    # free-text fields (eligibility, documents, selection, renewal, amount, income...):
    # the value may summarise, but most of its content words must come from the quote
    vw, qw = _content_words(value), _content_words(quote)
    if not vw:
        return False, "empty value"
    overlap = len(vw & qw) / len(vw)
    return overlap >= 0.6, f"{overlap:.0%} of value words in quote"


# ---------------------------------------------------------------- quote check
def quote_in_page(quote: str, page_text: str) -> float:
    """0-100 score for how well the quote occurs in the page."""
    nq, nt = norm(quote), norm(page_text)
    if not nq or len(nq) < 4:
        return 0.0
    if nq in nt:
        return 100.0
    return float(fuzz.partial_ratio(nq, nt)) if len(nq) <= 600 else 0.0


QUOTE_JOIN = " … "


@dataclass
class GroundedField:
    field: str
    value: str | None          # None means Not specified
    evidence: str | None       # quote(s) joined with QUOTE_JOIN
    grounded: bool
    match_score: float         # weakest quote's match score
    reason: str
    raw_value: str | None = None  # what the LLM claimed (kept for audit even when rejected)


def ground_field(field: str, value, evidence, page_text: str,
                 page_links: list[tuple[str, str]] | None = None) -> GroundedField:
    """`evidence` may be one quote or a list of up to 4 quotes (for values that summarise
    several sentences). EVERY quote must be on the page; the value is checked against
    all quotes together."""
    if value in (None, "", [], "null", "None") or str(value).strip().lower() in ("not specified", "n/a", "na"):
        return GroundedField(field, None, None, False, 0.0, "not stated on page")
    value = str(value).strip()
    quotes = [q.strip() for q in (evidence if isinstance(evidence, list) else [evidence])
              if isinstance(q, str) and q.strip()][:4]
    joined = QUOTE_JOIN.join(quotes)

    if field == "application_url":
        ok, why = value_supported(field, value, joined, page_links, page_text)
        return GroundedField(field, value if ok else None, joined or value, ok, 100.0 if ok else 0.0, why, value)

    if not quotes:
        return GroundedField(field, None, None, False, 0.0, "no evidence quote given -> rejected", value)
    scores = [quote_in_page(q, page_text) for q in quotes]
    score = min(scores)
    if score < config.FUZZY_MATCH_THRESHOLD:
        bad = quotes[scores.index(score)][:60]
        return GroundedField(field, None, joined, False, score,
                             f"quote not found on page (match {score:.0f}): \"{bad}\"", value)
    ok, why = value_supported(field, value, "\n".join(quotes), page_links, page_text)
    if not ok:
        return GroundedField(field, None, joined, False, score, f"quote found but {why}", value)
    return GroundedField(field, value, joined, True, score, why, value)


if __name__ == "__main__":
    page = ("Post Matric Scholarship for Scheduled Caste Students 2026-27. "
            "Parental income should not exceed Rs. 2,50,000 per annum. "
            "Last date of application: 31.10.2026. Only girl students are eligible. "
            "Apply online at scholarships.gov.in")
    tests = [
        ("close_date", "2026-10-31", "Last date of application: 31.10.2026"),       # good
        ("close_date", "2026-11-30", "Last date of application: 31.10.2026"),       # wrong date
        ("income_limit", "Rs. 2,50,000 per annum", "Parental income should not exceed Rs. 2,50,000 per annum"),
        ("income_limit", "Rs. 5,00,000 per annum", "Parental income should not exceed Rs. 2,50,000 per annum"),  # invented number
        ("income_limit", "Rs. 2,50,000", "Family income must be below Rs 2.5 lakh"),  # quote not on page
        ("gender", "Female", "Only girl students are eligible."),
        ("name", "Post Matric Scholarship for SC Students", "Post Matric Scholarship for Scheduled Caste Students"),
        ("amount", "Rs. 10,000", None),                                             # no evidence
        ("age_limit", None, None),                                                  # not stated
        ("application_url", "https://scholarships.gov.in", None),
    ]
    for f, v, e in tests:
        g = ground_field(f, v, e, page)
        print(f"{'OK  ' if g.grounded else 'NO  '} {f:15} {str(v):40} -> {g.value or config.NOT_SPECIFIED:25} ({g.reason})")
