"""Verification engine: a confidence score computed from evidence checks, in code.

No LLM produces or adjusts any number here. Each check reads stored evidence
(sources, snapshots, field_evidence, other records) and earns points by a fixed rule.
Weights live in config.SCORE_WEIGHTS / SCORE_PENALTIES. Every check's points and reason
are saved in score_breakdown, which is what the dashboard shows as "Why this score?".

confidence >= 95 -> VERIFIED, otherwise REVIEW_REQUIRED.
"""
import json
import re
from datetime import date, timedelta
from urllib.parse import urlparse

from rapidfuzz import fuzz

import classify
import config
import crawl
import db
import grounding

W = config.SCORE_WEIGHTS
P = config.SCORE_PENALTIES

ELIGIBILITY_FIELDS = ["income_limit", "category", "education_level", "academic_req", "gender", "age_limit",
                      "domicile", "institution_req", "courses", "disability", "eligibility_summary"]
KEY_COMPARE_FIELDS = ["close_date", "amount", "income_limit"]

# ---------------------------------------------------------------- provider ownership
_GOV = re.compile(r"\b(ministry|department|dept|government|govt|directorate|commission|council|board|"
                  r"aicte|ugc|nta|icssr|icmr|csir|dst|welfare|national testing|state|union|"
                  r"central sector|nsfdc|nbcfdc)\b", re.I)
_FOUNDATION = re.compile(r"\b(foundation|trust|charitable|society|ngo)\b", re.I)
_CORPORATE = re.compile(r"\b(limited|ltd|bank|group|industries|pvt|private|enterprises|companies|company)\b", re.I)
_UNIVERSITY = re.compile(r"\b(university|institute|college|iit|nit|iiit|iisc|iiser|vidyalaya|vishwavidyalaya)\b", re.I)
_GENERIC = {"foundation", "trust", "group", "limited", "education", "educational", "india", "indian",
            "companies", "company", "implemented", "along", "with", "the", "private", "ltd", "and", "for",
            "society", "charitable", "national", "bank"}


def provider_type(provider: str | None) -> str | None:
    if not provider:
        return None
    if _FOUNDATION.search(provider):
        return "foundation_trust"
    if _UNIVERSITY.search(provider) and not _GOV.search(provider):
        return "university"
    if _GOV.search(provider):
        return "government"
    if _CORPORATE.search(provider):
        return "corporate_csr"
    return None


# names that mark a government scheme even when the page does not name the provider
_GOV_SCHEME_NAME = re.compile(
    r"\b(national(?! institute)|central sector|pre[- ]?matric|post[- ]?matric|pm|pradhan mantri|prime minister'?s?|"
    r"ministry|nmms|government|govt|top class education|yasasvi|yashasvi|ugc|aicte|icmr|icar|dst|csir|"
    r"inspire|ishan uday)\b", re.I)


# blog / news / listicle pages: commentary about schemes, authoritative only for the site's own scheme
_COMMENTARY_PATH = re.compile(r"/(blogs?|news|articles?|examinfo|posts?)/|/(top|best)[-_]\d+[-_]|list[-_]of[-_]", re.I)


def provider_names_site(provider: str | None, url: str) -> bool:
    domain = classify.domain_of(url).replace(".", "").replace("-", "")
    tokens = [t for t in re.findall(r"[a-z]{3,}", (provider or "").lower()) if t not in _GENERIC
              and t not in {"university", "institute", "college", "global"}]
    return any(t in domain for t in tokens)


def inferred_provider_type(scheme_name: str | None) -> str | None:
    return "government" if scheme_name and _GOV_SCHEME_NAME.search(scheme_name) else None


def registered_root(domain: str) -> str:
    parts = domain.split(".")
    n = 3 if re.search(r"\.(gov|nic|ac|edu|co|org|res|ernet)\.in$", domain) else 2
    return ".".join(parts[-n:])


def provider_owns_domain(provider: str | None, source_type: str, url: str,
                         scheme_name: str | None = None) -> tuple[bool, str]:
    """Does the site this record came from belong to whoever runs the scheme?
    If the page does not name the provider, the scheme's own name decides: a 'National ...'
    or 'Post-Matric ...' scheme is a government scheme, so a university or coaching site
    writing about it is a third-party page, not the official source."""
    ptype = provider_type(provider)
    domain = classify.domain_of(url)
    if source_type not in classify.OFFICIAL_TYPES:
        return False, f"{domain} is a {source_type} site, not an official provider"
    # a government scheme's official source is a government site, whatever provider the page
    # seems to name (a university notice relaying an NSP scheme often shows only its own address)
    if inferred_provider_type(scheme_name) == "government" and source_type != "government":
        return False, f"scheme name marks a government scheme; {domain} ({source_type}) only relays it - third-party page"
    if source_type != "government" and _COMMENTARY_PATH.search(urlparse(url).path) \
            and not provider_names_site(provider, url):
        return False, f"{domain} page is a blog/news/list article about the scheme, not its provider - third-party page"
    if ptype is None:
        ptype = inferred_provider_type(scheme_name)
        if ptype is None:
            return True, f"provider not named separately; scheme is published on official {source_type} site {domain}"
        provider = f"(inferred from scheme name) {ptype}"
    if source_type == "government":
        ok = ptype == "government"
    elif source_type == "university":
        ok = ptype == "university"
    else:  # corporate / foundation: the provider's own name must be in the domain
        tokens = [t for t in re.findall(r"[a-z]{4,}", (provider or "").lower()) if t not in _GENERIC]
        ok = any(t in domain.replace(".", "") for t in tokens)
    if ok:
        return True, f"provider '{provider[:60]}' ({ptype}) runs the {source_type} site {domain}"
    return False, f"provider '{provider[:60]}' ({ptype}) does not own {domain} ({source_type}) - third-party page"


# ---------------------------------------------------------------- helpers
def _evidence(conn, sch_id) -> dict:
    return {r["field"]: r for r in conn.execute("SELECT * FROM field_evidence WHERE scholarship_id=?", (sch_id,))}


def _latest_snapshot_text(conn, url) -> str:
    r = conn.execute("SELECT text_path FROM snapshots WHERE url=? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
    try:
        return crawl.load_snapshot_text(r["text_path"]) if r else ""
    except FileNotFoundError:
        return ""


def _name_key(name: str) -> str:
    n = grounding.norm(name)
    n = re.sub(r"\b(19|20)\d{2}(\s*-\s*\d{2,4})?\b", " ", n)
    n = re.sub(r"\([^)]*\)", " ", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def _academic_years(today: date) -> list[str]:
    """Current and next academic year in the usual Indian spellings: 2026-27, 2026-2027 ..."""
    start = today.year if today.month >= 4 else today.year - 1
    out = []
    for y in (start - 1, start, start + 1):  # previous AY still valid early in the year
        out += [f"{y}-{str(y + 1)[2:]}", f"{y}-{y + 1}", f"{y}–{str(y + 1)[2:]}"]
    return out


_ROLLING = re.compile(r"(open throughout the year|throughout the year|rolling basis|all round the year|"
                      r"no last date|applications? (are|is) accepted (at )?any ?time)", re.I)


def _same_values(a: dict, b: dict, field: str) -> bool | None:
    """Compare a key field between two records. None = can't compare (missing on one side)."""
    va, vb = a.get(field), b.get(field)
    if not va or not vb or not va["grounded"] or not vb["grounded"]:
        return None
    if field.endswith("_date"):
        return va["value"] == vb["value"]
    na, nb = grounding.numbers_in(va["value"]), grounding.numbers_in(vb["value"])
    return bool(na & nb) if na and nb else None


# ---------------------------------------------------------------- the score
def score(conn, sch, today: date | None = None) -> tuple[float, list[dict], dict]:
    today = today or date.today()
    ev = _evidence(conn, sch["id"])
    src = db.get_source(conn, sch["official_url"])
    stype = src["source_type"] if src else "unknown"
    checks = []

    def add(key, earned, reason, maximum=None):
        maximum = maximum if maximum is not None else W.get(key, P.get(key, 0))
        checks.append({"check": key, "points": round(earned, 2), "max": maximum, "reason": reason})

    def g(f):
        r = ev.get(f)
        return r if r is not None and r["grounded"] else None

    provider_val = g("provider")["value"] if g("provider") else None
    owns, own_reason = provider_owns_domain(provider_val, stype, sch["official_url"], sch["name"])

    # 1. official source (the provider's own site)
    add("official_source", W["official_source"] if owns else 0, own_reason)

    # 2. scheme present on the official page in the latest crawl
    present = bool(g("name")) and src is not None and (src["fail_count"] or 0) == 0
    add("present_on_official_page", W["present_on_official_page"] if present else 0,
        "scheme name found in latest snapshot of the source page" if present else
        ("source page failed to load in the latest crawl" if src and src["fail_count"] else "name not grounded"))

    # 3. application URL on the provider's domain or a known portal
    app = g("application_url")
    if app:
        ad = classify.domain_of(app["value"])
        sd = classify.domain_of(sch["official_url"])
        portal = any(ad == p or ad.endswith("." + p) for p in config.KNOWN_APPLICATION_PORTALS)
        same = registered_root(ad) == registered_root(sd)
        gov_ok = stype == "government" and ad.endswith(config.GOVT_SUFFIXES)
        ok = same or portal or gov_ok
        add("application_url", W["application_url"] if ok else W["application_url"] / 2,
            f"apply link {ad} " + ("is on the provider's domain" if same else
                                   "is a known scholarship portal" if portal else
                                   "is a government portal" if gov_ok else "is on another domain (half points)"))
    else:
        add("application_url", 0, "no application URL stated on the page")

    # 4. eligibility grounded (scaled: 3+ grounded eligibility fields = full points)
    elig = [f for f in ELIGIBILITY_FIELDS if g(f)]
    add("eligibility_grounded", W["eligibility_grounded"] * min(len(elig), 3) / 3,
        f"{len(elig)} eligibility fields grounded ({', '.join(elig) or 'none'}); 3 needed for full points")

    # 5. deadline grounded (or the page explicitly says applications are rolling)
    page_text = _latest_snapshot_text(conn, sch["official_url"])
    if g("close_date"):
        add("deadline_grounded", W["deadline_grounded"], f"closing date {g('close_date')['value']} quoted from page")
    elif _ROLLING.search(page_text):
        add("deadline_grounded", W["deadline_grounded"], "page states applications are open throughout / rolling")
    else:
        add("deadline_grounded", 0, "no closing date stated on the page")

    # 6. amount grounded
    add("amount_grounded", W["amount_grounded"] if g("amount") else 0,
        "benefit amount quoted from page" if g("amount") else "benefit amount not stated")

    # 7. freshness: is this scheme's information current?
    ays = _academic_years(today)
    lo, hi = today - timedelta(days=30 * config.FRESHNESS_MONTHS), today + timedelta(days=540)
    scheme_text = " ".join(str(r["evidence_quote"] or "") + " " + str(r["value"] or "")
                           for r in ev.values() if r["grounded"])
    old_year = re.search(r"\b(20\d{2})(?:\s*[-–]\s*(\d{2,4}))?\b", sch["name"] or "")
    stale_name = old_year and int(old_year.group(1)) < int(ays[0][:4])  # named for a year before the previous AY
    scheme_dates = [d for d in grounding.find_dates(scheme_text) if lo <= d <= hi]
    if stale_name:
        add("freshness", 0, f"scheme is named for an old year ({old_year.group(0)})")
    elif scheme_dates or any(a in scheme_text for a in ays):
        add("freshness", W["freshness"], "scheme's own evidence has a current date/academic year")
    elif any(a in page_text for a in ays) or any(lo <= d <= hi for d in grounding.find_dates(page_text)):
        add("freshness", W["freshness"] / 2, "only the page (not the scheme text) shows a current date/year")
    else:
        add("freshness", 0, "no current date or academic year found")

    # 8 + penalties need the other records describing the same scheme
    key = _name_key(sch["name"])
    others = []
    for o in conn.execute("SELECT id, name, official_url, provider FROM scholarships WHERE id != ?", (sch["id"],)):
        if fuzz.token_sort_ratio(key, _name_key(o["name"])) >= 90:
            others.append(o)

    # 8. extraction consistency: other sources or earlier runs agree on key values
    agree, disagree_official, official_twin = [], [], None
    for o in others:
        oev = _evidence(conn, o["id"])
        osrc = db.get_source(conn, o["official_url"])
        o_owns, _ = provider_owns_domain(oev["provider"]["value"] if oev.get("provider") and oev["provider"]["grounded"] else None,
                                         osrc["source_type"] if osrc else "unknown", o["official_url"], o["name"])
        if o_owns:
            official_twin = official_twin or o["id"]
        cmp = [_same_values(ev, oev, f) for f in KEY_COMPARE_FIELDS]
        if any(c is False for c in cmp):
            if o_owns and owns:
                disagree_official.append((o["id"], [f for f, c in zip(KEY_COMPARE_FIELDS, cmp) if c is False]))
        elif any(c is True for c in cmp) or not any(c is not None for c in cmp):
            agree.append(o["id"])
    runs = conn.execute("SELECT COUNT(DISTINCT run_id) FROM snapshots WHERE url=? AND content_hash=?",
                        (sch["official_url"], src["content_hash"] if src else "")).fetchone()[0]
    if agree:
        add("extraction_consistency", W["extraction_consistency"],
            f"same scheme also found in record(s) {agree} with no conflicting key values")
    elif runs >= 2:
        add("extraction_consistency", W["extraction_consistency"], f"same page content re-confirmed in {runs} crawl runs")
    else:
        add("extraction_consistency", 0, "seen in one source and one run so far - not yet corroborated")

    # 9. field traceability: share of claimed values that grounded
    claimed = [r for r in ev.values() if r["raw_value"] is not None]
    grounded_n = sum(1 for r in claimed if r["grounded"])
    frac = grounded_n / len(claimed) if claimed else 0
    add("field_traceability", W["field_traceability"] * frac, f"{grounded_n}/{len(claimed)} extracted values traced to quotes")

    # penalties
    if disagree_official:
        add("conflicting_official_source", P["conflicting_official_source"],
            "another official source disagrees: " + "; ".join(f"#{i} on {', '.join(fs)}" for i, fs in disagree_official), 0)
    if not owns:
        add("aggregator_only", P["aggregator_only"],
            "evidence comes only from a third-party page" +
            (f"; official record is #{official_twin}" if official_twin else "; no official provider page found yet"), 0)

    total = max(0.0, min(100.0, sum(c["points"] for c in checks)))
    meta = {"owns_source": owns, "official_twin": official_twin, "conflicts": disagree_official}
    return round(total, 1), checks, meta


def verify_all(conn, today: date | None = None) -> dict:
    counts = {"VERIFIED": 0, "REVIEW_REQUIRED": 0}
    for sch in conn.execute("SELECT * FROM scholarships").fetchall():
        conf, checks, meta = score(conn, sch, today)
        vstatus = "VERIFIED" if conf >= config.VERIFIED_THRESHOLD else "REVIEW_REQUIRED"
        counts[vstatus] += 1
        present = next(c for c in checks if c["check"] == "present_on_official_page")["points"] > 0
        conn.execute(
            """UPDATE scholarships SET confidence=?, verification_status=?, score_breakdown=?,
               last_verified=CASE WHEN ? THEN ? ELSE last_verified END WHERE id=?""",
            (conf, vstatus, json.dumps({"checks": checks, **meta}, ensure_ascii=False),
             int(present), db.now_iso(), sch["id"]),
        )
    return counts


if __name__ == "__main__":
    db.init_db()
    with db.session() as conn:
        print(verify_all(conn))
