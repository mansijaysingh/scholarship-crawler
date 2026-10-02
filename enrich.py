"""Evidence enrichment: fill a record's missing fields from the provider's own linked pages.

Summary pages (e.g. a ministry's list of schemes) rarely carry deadlines or apply links;
those sit on each scheme's detail page or guideline PDF. For an official record that is
missing key fields, we follow up to 3 links from its source page that belong to the same
provider AND point at this scheme, extract them with the same prompt, ground them with
the same rules, and fill ONLY fields that are still Not specified. Each filled field keeps
its own source URL, snapshot and quote, so the trace stays Database -> Source -> Evidence.
"""
import json
import logging
import re

from rapidfuzz import fuzz

import classify
import crawl
import db
import extract
import verify

log = logging.getLogger("enrich")

TARGET_FIELDS = ["close_date", "application_url", "amount"]
MAX_LINKS_PER_RECORD = 3

_GENERIC_NAME_WORDS = set("""aicte ugc scholarship scholarships scheme schemes fellowship fellowships the for of and
in to students student national central sector india indian govt government ministry special programme program
award awards grant financial assistance higher education young merit means cum pg ug post pre matric metric
academic initiative initiatives skill skills holistic venture achievers research assets social service services
basis performance activities activity state board based other university institute college ideas category
union territories scheduled caste undergraduate postgraduate graduate doctoral girls women wards abled specially
students college universities support development""".split())
_FILE_WORDS = {"pdf", "doc", "docx", "html", "php", "aspx", "www", "http", "https"}
_SKIP_LINK = re.compile(r"batch|merit[ _%-]?list|user[ _%-]?man|result|selected|sanction|list of|archive|"
                        r"faculty|tender|recruit|vacanc|screen-reader|login|gazette", re.I)
_USEFUL_LINK = re.compile(r"general[-_ ]?instruction|guideline|guidlines|advertisement|apply|application|faq|"
                          r"eligib|details|read more|know more|scheme[-_ ]document|brochure|notification", re.I)
# a page with ONE scheme may use generic links, but only these explicit kinds
_SINGLE_SCHEME_LINK = re.compile(r"guideline|guidlines|apply|application|faq|eligib|brochure|advertisement", re.I)
_OLD_YEAR = re.compile(r"(?<!\d)(19\d{2}|20[01]\d|202[0-3])(?!\d)")


def distinctive_tokens(name: str) -> list[str]:
    """Words that identify this scheme in a URL/anchor, e.g. 'pragati', 'yashasvi', 'adf'."""
    acronyms = re.findall(r"\(([A-Za-z]{2,10})\)", name or "")
    words = re.findall(r"[a-z]{4,}", (name or "").lower())
    toks = [a.lower() for a in acronyms] + [w for w in words if w not in _GENERIC_NAME_WORDS]
    return [t for t in dict.fromkeys(toks) if t not in _FILE_WORDS and t not in _GENERIC_NAME_WORDS]


def _has_word(token: str, hay: str) -> bool:
    return bool(re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", hay))


def link_allowed(href: str, source_url: str, provider: str | None) -> bool:
    """Only the provider's own web presence: same site, another official domain,
    or a domain that carries the provider's name (e.g. aicte-india.org for AICTE)."""
    d, sd = classify.domain_of(href), classify.domain_of(source_url)
    if verify.registered_root(d) == verify.registered_root(sd):
        return True
    stype, official, _ = classify.classify(href)
    if official and stype == classify.classify(source_url)[0]:
        return True
    ptoks = [t for t in re.findall(r"[a-z]{4,}", (provider or "").lower()) if t not in verify._GENERIC]
    ptoks += [verify.registered_root(sd).split(".")[0]]  # e.g. 'aicte' from aicte.gov.in
    return any(len(t) >= 4 and t in d.replace(".", "").replace("-", "") for t in ptoks)


def candidate_links(name: str, page_links, source_url: str, provider: str | None, single_scheme: bool,
                    app_url: str | None = None):
    # tokens that are part of the site's own domain (e.g. 'reliance', 'sharda') match every link: drop them
    site = classify.domain_of(source_url).replace(".", " ").replace("-", " ")
    toks = [t for t in distinctive_tokens(name) if not _has_word(t, site)]
    scored = []
    if app_url and link_allowed(app_url, source_url, provider):
        full = app_url if "://" in app_url else "https://" + app_url
        scored.append((9, full, "application link stated on the source page"))
    for href, anchor in page_links:
        if crawl.normalize_url(href) == crawl.normalize_url(source_url):
            continue
        path = re.sub(r"^https?://[^/]+", "", href)  # match on path + anchor, never the domain
        hay = f"{path} {anchor}".lower().replace("%20", " ").replace("_", " ").replace("-", " ")
        if _SKIP_LINK.search(hay) or _OLD_YEAR.search(hay) or not link_allowed(href, source_url, provider):
            continue
        named = any(_has_word(t, hay) for t in toks)
        if named:
            scored.append((2 + bool(_USEFUL_LINK.search(hay)), href, anchor))
        elif single_scheme and _SINGLE_SCHEME_LINK.search(hay):
            scored.append((1, href, anchor))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _s, href, anchor in scored:
        if href not in seen:
            seen.add(href)
            out.append((href, anchor))
    return out[:MAX_LINKS_PER_RECORD]


def needs_enrichment(conn, sch) -> list[str]:
    ev = {r["field"]: r for r in conn.execute("SELECT field, grounded FROM field_evidence WHERE scholarship_id=?", (sch["id"],))}
    missing = [f for f in TARGET_FIELDS if not (ev.get(f) and ev[f]["grounded"])]
    elig = sum(1 for f in verify.ELIGIBILITY_FIELDS if ev.get(f) and ev[f]["grounded"])
    if elig < 3:
        missing.append("eligibility")
    return missing


def _match_scheme(name: str, schemes: list[extract.ExtractedScheme]):
    key = verify._name_key(name)
    toks = distinctive_tokens(name)
    best, best_score = None, 0
    for s in schemes:
        sc = fuzz.token_set_ratio(key, verify._name_key(s.name))
        if toks and any(t in s.name.lower() for t in toks):
            sc = max(sc, 85)
        if sc > best_score:
            best, best_score = s, sc
    return best if best_score >= 80 else None


_page_cache: dict[str, crawl.Page] = {}


def links_for(conn, sch) -> list[tuple[str, str]]:
    url = sch["official_url"]
    if url not in _page_cache:
        _page_cache[url] = crawl.fetch(url)
    src_page = _page_cache[url]
    if not src_page.ok:
        return []
    n_on_page = conn.execute("SELECT COUNT(*) FROM scholarships WHERE official_url=?", (url,)).fetchone()[0]
    app = conn.execute("SELECT value FROM field_evidence WHERE scholarship_id=? AND field='application_url' AND grounded=1",
                       (sch["id"],)).fetchone()
    return candidate_links(sch["name"], src_page.links, url, sch["provider"], n_on_page == 1, app[0] if app else None)


def enrich_record(conn, sch, run_id: int) -> list[str]:
    """Returns the fields filled for this record."""
    links = links_for(conn, sch)
    filled_all = []
    for href, anchor in links:
        page = crawl.fetch(href)
        if not page.ok:
            log.info("      x %s (%s)", href, page.error)
            continue
        done = conn.execute("SELECT 1 FROM enrich_log WHERE scholarship_id=? AND url=? AND content_hash=?",
                            (sch["id"], href, page.content_hash)).fetchone()
        if done:
            continue
        stype, official, _ = classify.classify(href, page.text)
        db.upsert_source(conn, href, classify.domain_of(href), stype, official, f"enrich:{sch['official_url']}", 1)
        conn.execute("UPDATE sources SET last_crawled=?, http_status=?, content_hash=?, is_relevant=1 WHERE url=?",
                     (db.now_iso(), page.status, page.content_hash, href))
        snap_id, _ = crawl.record_snapshot(conn, page, run_id)
        schemes = extract.extract_page(page.text, page.url, page.title, page.links)  # may raise on quota
        match = _match_scheme(sch["name"], schemes)
        filled = merge_missing(conn, sch["id"], match, href, snap_id, run_id) if match else []
        conn.execute("INSERT INTO enrich_log (scholarship_id, url, content_hash, run_id, attempted_at, filled_fields, note) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (sch["id"], href, page.content_hash, run_id, db.now_iso(), json.dumps(filled),
                      f"anchor '{anchor[:60]}'; " + (f"matched '{match.name[:60]}'" if match else
                                                     f"scheme not found among {len(schemes)} extracted")))
        conn.commit()
        log.info("      %s %s -> %s", "+" if filled else "-", href[:90], filled or "nothing new")
        filled_all += filled
    return filled_all


def merge_missing(conn, sch_id: int, scheme: extract.ExtractedScheme, url: str, snap_id: int, run_id: int) -> list[str]:
    """Fill fields that are Not specified in the record. Never overwrite a grounded value."""
    existing = {r["field"]: r for r in conn.execute("SELECT field, grounded FROM field_evidence WHERE scholarship_id=?", (sch_id,))}
    filled = []
    for f, g in scheme.fields.items():
        if f == "name" or not g.grounded or (existing.get(f) and existing[f]["grounded"]):
            continue
        conn.execute(f"UPDATE scholarships SET {f}=? WHERE id=?", (g.value, sch_id))
        conn.execute(
            """INSERT INTO field_evidence (scholarship_id, field, value, evidence_quote, source_url, snapshot_id,
                   grounded, match_score, raw_value, reason, model, run_id)
               VALUES (?,?,?,?,?,?,1,?,?,?,?,?)
               ON CONFLICT(scholarship_id, field) DO UPDATE SET value=excluded.value,
                   evidence_quote=excluded.evidence_quote, source_url=excluded.source_url,
                   snapshot_id=excluded.snapshot_id, grounded=1, match_score=excluded.match_score,
                   raw_value=excluded.raw_value, reason=excluded.reason, model=excluded.model, run_id=excluded.run_id""",
            (sch_id, f, g.value, g.evidence, url, snap_id, g.match_score, g.raw_value,
             f"enriched from linked provider page: {g.reason}", scheme.model, run_id))
        filled.append(f)
    if scheme.eligibility:
        cur = json.loads(conn.execute("SELECT eligibility_json FROM scholarships WHERE id=?", (sch_id,)).fetchone()[0] or "{}")
        merged = {**scheme.eligibility, **cur}  # existing values win
        conn.execute("UPDATE scholarships SET eligibility_json=? WHERE id=?", (db.to_json(merged), sch_id))
    return filled


def enrich_all(conn, run_id: int) -> dict:
    stats = {"records": 0, "filled_records": 0, "fields_filled": 0, "stopped_on_quota": False}
    rows = conn.execute("SELECT * FROM scholarships ORDER BY id").fetchall()
    for sch in rows:
        meta = json.loads(sch["score_breakdown"] or "{}")
        if not meta.get("owns_source"):
            continue  # third-party copies are not enriched; their official twin is
        missing = needs_enrichment(conn, sch)
        if not missing:
            continue
        stats["records"] += 1
        log.info("  #%d %s  missing %s", sch["id"], sch["name"][:60], missing)
        try:
            filled = enrich_record(conn, sch, run_id)
        except extract.LLMQuotaExhausted as e:  # no model left: stop; the rest is retried next run
            log.warning("  LLM quota exhausted, enrichment paused: %s", str(e)[:150])
            stats["stopped_on_quota"] = True
            break
        except extract.LLMTemporaryError as e:  # overloaded: skip this record, retried next run
            log.warning("  LLM busy, record skipped: %s", str(e)[:120])
            continue
        if filled:
            stats["filled_records"] += 1
            stats["fields_filled"] += len(filled)
    return stats
