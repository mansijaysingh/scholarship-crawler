"""Archive baseline for change detection (build plan section 8.3).

Official pages change between scheme cycles (new deadline, revised amount), but two runs
a day apart rarely see that. So for official records with grounded key facts, we fetch the
Internet Archive (Wayback Machine, free) copy of the SAME official page from about a year
ago, extract it with the same prompt and grounding rules, and compare it with today's record.

Every difference is logged in change_log exactly like a crawl-to-crawl change, but labelled
'baseline: web.archive.org snapshot dated YYYY-MM-DD' and with both quotes (archived + current)
as evidence. Nothing is edited by hand, and today's record values are not modified: the
archive is OLDER, so the archived value is the old value and today's grounded value the new one.
"""
import json
import logging
import time
from datetime import date, datetime

import requests

import config
import crawl
import db
import enrich
import extract
import grounding

log = logging.getLogger("baseline")

CDX = "https://web.archive.org/cdx/search/cdx"
COMPARE_FIELDS = ["close_date", "open_date", "amount", "income_limit", "academic_req", "age_limit"]
MIN_AGE_DAYS, MAX_AGE_DAYS, TARGET_AGE_DAYS = 60, 1000, 365


def archived_versions(url: str) -> list[str]:
    """Timestamps of distinct archived versions (HTTP 200) of a URL. The CDX index API
    sometimes answers empty/429 under load, so retry before concluding there is no archive."""
    for attempt in range(3):
        try:
            r = requests.get(CDX, params={"url": url, "output": "json", "filter": "statuscode:200",
                                          "collapse": "digest", "fl": "timestamp", "from": "2023"},
                             headers={"User-Agent": config.USER_AGENT}, timeout=60)
            if r.ok and r.text.strip():
                return [row[0] for row in r.json()[1:]]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(10 * (attempt + 1))
    return []


def already_baselined(conn, url: str) -> bool:
    return conn.execute("SELECT 1 FROM snapshots WHERE url LIKE 'https://web.archive.org/web/%' AND url LIKE ? "
                        "AND label LIKE 'baseline:%'", ("%/" + url,)).fetchone() is not None


def pick_version(timestamps: list[str], today: date) -> str | None:
    """The version closest to one year old, between 2 months and ~2.7 years old."""
    best, best_gap = None, None
    for ts in timestamps:
        age = (today - datetime.strptime(ts[:8], "%Y%m%d").date()).days
        if MIN_AGE_DAYS <= age <= MAX_AGE_DAYS:
            gap = abs(age - TARGET_AGE_DAYS)
            if best_gap is None or gap < best_gap:
                best, best_gap = ts, gap
    return best


def candidates(conn, limit: int) -> list[str]:
    """Official HTML pages whose records have grounded deadlines/amounts (PDF URLs are
    usually versioned by file name, so they have no older copy of the same URL)."""
    urls = []
    for r in conn.execute(
            """SELECT s.official_url AS url, COUNT(*) AS n FROM scholarships s
               JOIN field_evidence fe ON fe.scholarship_id = s.id
               WHERE fe.grounded = 1 AND fe.field IN ('close_date','amount','income_limit')
                 AND fe.source_url = s.official_url
               GROUP BY s.official_url ORDER BY n DESC"""):
        own = conn.execute("SELECT score_breakdown FROM scholarships WHERE official_url=? LIMIT 1", (r["url"],)).fetchone()
        if r["url"].lower().endswith(".pdf") or not json.loads(own[0] or "{}").get("owns_source"):
            continue
        urls.append(r["url"])
    return urls[:limit]


def compare(conn, sch, archived: extract.ExtractedScheme, archive_url: str, label: str,
            current_text: str, run_id: int) -> list[dict]:
    cur = {r["field"]: r for r in conn.execute(
        "SELECT * FROM field_evidence WHERE scholarship_id=? AND grounded=1", (sch["id"],))}
    logged = []
    for f in COMPARE_FIELDS:
        old, new = archived.fields[f], cur.get(f)
        if not old.grounded or new is None or new["source_url"] != sch["official_url"]:
            continue  # need the fact stated on both versions of the same official page
        if f.endswith("_date"):
            changed = old.value != new["value"]
        else:
            old_quote_gone = all(grounding.quote_in_page(q, current_text) < config.FUZZY_MATCH_THRESHOLD
                                 for q in old.evidence.split(grounding.QUOTE_JOIN))
            changed = old_quote_gone and grounding.numbers_in(old.value) != grounding.numbers_in(new["value"])
        if not changed:
            continue
        dup = conn.execute("SELECT 1 FROM change_log WHERE scholarship_id=? AND field=? AND old_value=? AND new_value=? "
                           "AND note LIKE 'baseline:%'", (sch["id"], f, old.value, new["value"])).fetchone()
        if dup:
            continue
        evidence = f"OLD ({archive_url}): \"{old.evidence[:300]}\" | NEW (official page): \"{(new['evidence_quote'] or '')[:300]}\""
        conn.execute(
            """INSERT INTO change_log (scholarship_id, field, old_value, new_value, detected_at, source_url,
                   evidence_quote, run_id, note) VALUES (?,?,?,?,?,?,?,?,?)""",
            (sch["id"], f, old.value, new["value"], db.now_iso(), sch["official_url"], evidence, run_id, label))
        conn.execute("UPDATE scholarships SET last_changed=? WHERE id=?", (db.now_iso(), sch["id"]))
        logged.append({"field": f, "old": old.value, "new": new["value"]})
    return logged


def run_baseline(max_pages: int = 12) -> None:
    db.init_db()
    today = date.today()
    with db.session() as conn:
        run_id = db.start_run(conn)
        conn.commit()
        stats = {"pages": 0, "changes": 0, "no_archive": 0}
        for url in candidates(conn, max_pages):
            if already_baselined(conn, url):
                log.info("  = already compared with its archive: %s", url)
                continue
            time.sleep(3)  # be gentle with the archive's index API
            ts = pick_version(archived_versions(url), today)
            if not ts:
                stats["no_archive"] += 1
                log.info("  - no suitable archived version: %s", url)
                continue
            archive_url = f"https://web.archive.org/web/{ts}id_/{url}"  # id_ = original page, no archive toolbar
            label = f"baseline: web.archive.org snapshot dated {ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            page = crawl.fetch(archive_url)
            if not page.ok:
                log.info("  x archive fetch failed %s (%s)", archive_url, page.error)
                continue
            stats["pages"] += 1
            db.upsert_source(conn, archive_url, "web.archive.org", "archive", False, f"baseline:{url}", 0)
            crawl.save_snapshot(conn, page, run_id, label=label)
            try:
                schemes = extract.extract_page(page.text, url, page.title, page.links)
            except (extract.LLMQuotaExhausted, extract.LLMTemporaryError) as e:
                log.warning("  LLM unavailable, baseline stopped: %s", str(e)[:120])
                break
            current_snap = conn.execute("SELECT text_path FROM snapshots WHERE url=? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
            current_text = crawl.load_snapshot_text(current_snap["text_path"]) if current_snap else ""
            log.info("  %s  [%s]  %d schemes in archived copy", url, label, len(schemes))
            for sch in conn.execute("SELECT * FROM scholarships WHERE official_url=?", (url,)).fetchall():
                match = enrich._match_scheme(sch["name"], schemes)
                if not match:
                    continue
                for ch in compare(conn, sch, match, archive_url, label, current_text, run_id):
                    stats["changes"] += 1
                    log.info("      CHANGE DETECTED #%d %s: %r -> %r", sch["id"], ch["field"], ch["old"], ch["new"])
            conn.commit()
        db.finish_run(conn, run_id, pages_crawled=stats["pages"], updated=stats["changes"],
                      notes=f"archive baseline: {stats}")
        log.info("baseline done: %s", stats)
