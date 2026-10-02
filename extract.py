"""Extraction: the LLM *reads* a page and returns {value, evidence} per field.
It never decides anything: every value is then checked by grounding.py in code,
and anything unsupported is stored as Not specified.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types

import classify
import config
import db
import grounding
from grounding import GroundedField

log = logging.getLogger("extract")

FIELD_SPECS = {
    "name": "official name of the scholarship/fellowship scheme",
    "provider": ("organisation that offers/funds/implements the scheme. Evidence must be text that shows "
                 "this (e.g. 'The Institute gives...', 'Ministry of X implements...', or the page title/header "
                 "of the provider's own site). A name that only appears in a contact list is NOT evidence"),
    "application_url": "URL where students apply, ONLY if written on the page or given as a link",
    "amount": "scholarship amount / benefit as written (e.g. 'Rs. 12,000 per annum', 'full tuition fee waiver')",
    "education_level": "level(s) of study covered (e.g. 'Class 11-12', 'UG', 'PG', 'PhD', 'Diploma')",
    "courses": "specific courses/streams covered, if stated",
    "academic_req": "minimum marks / merit requirement",
    "income_limit": "family/parental annual income ceiling",
    "age_limit": "age criteria",
    "gender": "gender restriction, if any (e.g. 'Female')",
    "category": "social category restriction (SC/ST/OBC/EWS/Minority/PwD etc.)",
    "domicile": "state / domicile / nationality requirement",
    "institution_req": "type of institution the student must be enrolled in",
    "disability": "disability-related condition, if any",
    "eligibility_summary": "one-sentence summary of who is eligible, using the page's own words",
    "open_date": "date applications open, as YYYY-MM-DD",
    "close_date": "last date / deadline to apply, as YYYY-MM-DD",
    "documents": "documents required",
    "selection_process": "how candidates are selected",
    "renewal": "renewal conditions",
}

STRUCTURED_SPEC = {
    "max_family_income_inr": "integer rupees per year, e.g. 250000, or null",
    "min_percentage": "number, minimum marks percentage, or null",
    "max_age": "integer, or null",
    "gender": "'female' | 'male' | null",
    "categories": ("list from [SC, ST, OBC, EBC, DNT, EWS, MINORITY, PWD, GENERAL] that the scheme is "
                   "RESTRICTED to (a reservation quota inside an open scheme is not a restriction), or []"),
    "levels": "list from [SCHOOL, CLASS_11_12, DIPLOMA, UG, PG, PHD, POSTDOC], or []",
    "domicile_states": "list of Indian state names, or []",
}

PROMPT = """You extract scholarship information for Indian students from ONE web page.

STRICT RULES
- Use ONLY the page text below. Never use outside knowledge, never guess, never fill defaults.
- If the page does not state a field, set it to {{"value": null, "evidence": null}}.
- "value" must use the page's own wording. Do not abbreviate, translate or reword
  (write "College and Graduation Students", not "UG").
- "evidence" is a LIST of 1-4 quotes, each copied EXACTLY, character for character, from the page
  text: the shortest sentences or table rows (max 400 characters each) that together state the
  value. Use several quotes when the value combines several sentences. Never paraphrase evidence.
- Dates: give value as YYYY-MM-DD only if a full day/month/year is written; otherwise null.
- One page may describe several distinct schemes: return one object per scheme.
- Only include actual scholarship/fellowship/financial-assistance schemes that the page describes
  with at least some details. Ignore schemes that are only mentioned by name in a menu or list.
- If there is no such scheme, return {{"scholarships": []}}.

FIELDS (each is {{"value": string|null, "evidence": [string, ...]|null}}):
{fields}

Also per scheme, "structured": machine-readable eligibility, filled ONLY from the same evidence:
{structured}

Return JSON: {{"scholarships": [{{"name": {{...}}, ..., "structured": {{...}}}}]}}

PAGE URL: {url}
PAGE TITLE: {title}
PAGE TEXT:
<<<
{text}
>>>"""


# ---------------------------------------------------------------- LLM call
_client = None
_last_call = 0.0


def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


_dead_models: dict[str, str] = {}  # model -> reason; skipped for the rest of this process


class LLMQuotaExhausted(RuntimeError):
    """Every model is out of daily quota or retired: stop extracting for this run."""


class LLMTemporaryError(RuntimeError):
    """Models are overloaded right now (503): skip this page, it is retried next run."""


def _retry_delay(err: Exception, default: float = 20.0) -> float:
    m = re.search(r"retryDelay'?:\s*'(\d+(?:\.\d+)?)s'", str(err))
    return min(float(m.group(1)) + 1, 90.0) if m else default


def call_llm(prompt: str) -> tuple[dict, str]:
    """Returns (parsed JSON, model used). temperature=0 for repeatable extraction.
    - daily free quota used up (429 PerDay) or model retired (404): model is skipped for
      the rest of the run, next model tried immediately
    - per-minute limit (429): wait the delay the API asks for, retry
    - overloaded (503): wait 20 s / 40 s and retry, then try the next model
    Raises LLMQuotaExhausted when no usable model is left, LLMTemporaryError otherwise."""
    global _last_call
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)
    last_err = None
    for model in config.GEMINI_MODELS:
        if model in _dead_models:
            continue
        for attempt in range(3):
            wait = config.LLM_MIN_SECONDS_BETWEEN_CALLS - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
            try:
                r = _get_client().models.generate_content(model=model, contents=prompt, config=cfg)
                return json.loads(r.text), model
            except errors.ClientError as e:
                last_err = e
                if e.code == 429 and "PerDay" not in str(e):
                    time.sleep(_retry_delay(e))
                    continue
                _dead_models[model] = "daily quota exhausted" if e.code == 429 else f"HTTP {e.code}"
                log.warning("    model %s unavailable for this run (%s)", model, _dead_models[model])
                break
            except errors.ServerError as e:
                last_err = e
                if attempt < 2:
                    time.sleep(20 * (attempt + 1))
            except json.JSONDecodeError as e:
                last_err = e
                break
    if all(m in _dead_models for m in config.GEMINI_MODELS):
        raise LLMQuotaExhausted(f"no usable model left: {_dead_models}")
    raise LLMTemporaryError(f"models busy/failing ({str(last_err)[:150]}); dead: {list(_dead_models)}")


# ---------------------------------------------------------------- text selection
_KW_LINE = re.compile("|".join(re.escape(k) for k in config.SCHOLARSHIP_KEYWORDS + config.DETAIL_KEYWORDS), re.I)


def select_text(text: str, limit: int = config.LLM_MAX_INPUT_CHARS) -> str:
    """Whole page if it fits; otherwise windows of lines around scholarship keywords."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    keep = set()
    for i, ln in enumerate(lines):
        if _KW_LINE.search(ln):
            keep.update(range(max(0, i - 8), min(len(lines), i + 25)))
    out, size = [], 0
    for i in sorted(keep):
        if size + len(lines[i]) > limit:
            break
        out.append(lines[i])
        size += len(lines[i]) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- extraction
_FUNDING_TERM = re.compile(
    r"scholarship|fellowship|financial (assistance|aid|support)|freeship|fee (waiver|concession|reimbursement)|"
    r"tuition waiver|\bgrants?\b|\baward\b|bursary|stipend|chhatravritti|chhatravriti", re.I)


def is_funding_scheme(name: str, amount_grounded: bool) -> bool:
    """A record must be a funding opportunity, judged from its own name (rule in code):
    - a funding term (scholarship, fellowship, freeship, grant, ...) -> yes
    - a generic 'scheme' -> only if a benefit amount is grounded on the page
    - anything else (course rows of a fee table, internships, programmes) -> no"""
    if _FUNDING_TERM.search(name or ""):
        return True
    return bool(re.search(r"\bscheme\b", name or "", re.I)) and amount_grounded


@dataclass
class ExtractedScheme:
    fields: dict[str, GroundedField]
    eligibility: dict
    model: str
    rejected: list[str] = field(default_factory=list)  # fields the LLM filled but grounding refused

    @property
    def name(self) -> str | None:
        return self.fields["name"].value


def _verify_structured(s: dict, f: dict[str, GroundedField]) -> dict:
    """Keep a structured value only if it is supported by that field's grounded evidence."""
    def ev(name):
        g = f.get(name)
        return g.evidence if g and g.grounded else ""

    out = {}
    inc = s.get("max_family_income_inr")
    if isinstance(inc, (int, float)) and ev("income_limit") and \
            any(abs(inc - n) < 1 for n in grounding.numbers_in(ev("income_limit"))):
        out["max_family_income_inr"] = int(inc)
    pct = s.get("min_percentage")
    if isinstance(pct, (int, float)) and ev("academic_req") and pct in grounding.numbers_in(ev("academic_req")):
        out["min_percentage"] = pct
    age = s.get("max_age")
    if isinstance(age, (int, float)) and ev("age_limit") and age in grounding.numbers_in(ev("age_limit")):
        out["max_age"] = int(age)
    if s.get("gender") in ("female", "male") and f["gender"].grounded:
        out["gender"] = s["gender"]
    cat_ev = grounding.norm(ev("category"))  # category field only: a quota in the summary is not a restriction
    syn = {"SC": ["scheduled caste", "sc"], "ST": ["scheduled tribe", "st"], "OBC": ["obc", "backward class"],
           "EBC": ["ebc", "extremely backward", "economically backward"], "DNT": ["dnt", "de-notified", "denotified"],
           "EWS": ["ews", "economically weaker"], "MINORITY": ["minorit"], "PWD": ["disab", "pwd", "divyang"],
           "GENERAL": ["general"]}
    cats = [c for c in s.get("categories") or [] if c in syn
            and any(re.search(rf"\b{t}", cat_ev) for t in syn[c])]
    if cats:
        out["categories"] = cats
    levels = [lv for lv in s.get("levels") or [] if lv in
              {"SCHOOL", "CLASS_11_12", "DIPLOMA", "UG", "PG", "PHD", "POSTDOC"}]
    if levels and f["education_level"].grounded:
        out["levels"] = levels
    states_ev = grounding.norm(ev("domicile"))
    states = [st for st in s.get("domicile_states") or [] if isinstance(st, str) and grounding.norm(st) in states_ev]
    if states:
        out["domicile_states"] = states
    return out


def extract_page(page_text: str, url: str, title: str, links: list[tuple[str, str]]) -> list[ExtractedScheme]:
    prompt = PROMPT.format(
        fields="\n".join(f"- {k}: {v}" for k, v in FIELD_SPECS.items()),
        structured="\n".join(f"- {k}: {v}" for k, v in STRUCTURED_SPEC.items()),
        url=url, title=title, text=select_text(page_text),
    )
    data, model = call_llm(prompt)
    ground_text = f"{title}\n{page_text}"  # title counts as page text (provider's own site header)
    schemes = []
    for raw in (data.get("scholarships") or []):
        if not isinstance(raw, dict):
            continue
        fields, rejected = {}, []
        for f in FIELD_SPECS:
            item = raw.get(f) or {}
            if not isinstance(item, dict):
                item = {"value": item, "evidence": None}
            g = grounding.ground_field(f, item.get("value"), item.get("evidence"), ground_text, links)
            if item.get("value") not in (None, "") and not g.grounded:
                rejected.append(f"{f}: {g.reason}")
            fields[f] = g
        if not fields["name"].grounded:  # a scheme whose name isn't on the page is not a record
            log.info("    dropped scheme %r: name not grounded (%s)", (raw.get("name") or {}).get("value"),
                     fields["name"].reason)
            continue
        if not is_funding_scheme(fields["name"].value, fields["amount"].grounded):
            log.info("    dropped %r: not a scholarship/funding scheme by name", fields["name"].value)
            continue
        schemes.append(ExtractedScheme(fields, _verify_structured(raw.get("structured") or {}, fields),
                                       model, rejected))
    return schemes


# ---------------------------------------------------------------- storage
def dedup_key(name: str, url: str) -> str:
    n = grounding.norm(name)
    n = re.sub(r"\b(19|20)\d{2}(\s*-\s*\d{2,4})?\b", " ", n)       # drop academic years
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return f"{n}|{classify.domain_of(url)}"


def store_new(conn, scheme: ExtractedScheme, url: str, source_type: str, snapshot_id: int, run_id: int) -> int | None:
    """Insert a newly found scholarship with its evidence rows. Returns id, or None if a
    record with the same key already exists (existing records are handled by change
    detection, which never silently overwrites)."""
    key = dedup_key(scheme.name, url)
    if db.find_by_key(conn, key):
        return None
    now = db.now_iso()
    values = {f: g.value for f, g in scheme.fields.items()}
    cols = ["dedup_key", "official_url", "provider_type", "eligibility_json", "first_seen", "last_seen_run"] + list(values)
    vals = [key, url, source_type, db.to_json(scheme.eligibility), now, run_id] + list(values.values())
    cur = conn.execute(f"INSERT INTO scholarships ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    sch_id = cur.lastrowid
    write_evidence(conn, sch_id, scheme, url, snapshot_id, run_id)
    return sch_id


def write_evidence(conn, sch_id: int, scheme: ExtractedScheme, url: str, snapshot_id: int, run_id: int) -> None:
    for f, g in scheme.fields.items():
        conn.execute(
            """INSERT INTO field_evidence
               (scholarship_id, field, value, evidence_quote, source_url, snapshot_id, grounded, match_score,
                raw_value, reason, model, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scholarship_id, field) DO UPDATE SET
                 value=excluded.value, evidence_quote=excluded.evidence_quote, source_url=excluded.source_url,
                 snapshot_id=excluded.snapshot_id, grounded=excluded.grounded, match_score=excluded.match_score,
                 raw_value=excluded.raw_value, reason=excluded.reason, model=excluded.model, run_id=excluded.run_id""",
            (sch_id, f, g.value, g.evidence, url, snapshot_id, int(g.grounded), g.match_score,
             g.raw_value, g.reason, scheme.model, run_id),
        )
