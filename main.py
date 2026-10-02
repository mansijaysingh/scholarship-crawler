"""Scholarship Intelligence Crawler - one full crawl cycle per run.

    python main.py run                 # full cycle (schedule this: cron / Task Scheduler / GitHub Actions)
    python main.py run --max-pages 80  # smaller discovery budget
    python main.py status              # summary of the database

A run:
  1. RE-CHECK   every official page that already has records (independent of search results)
  2. DISCOVER   new pages: seeds + web search + scholarship-aware link following
  3. EXTRACT    new/changed official pages -> GROUND -> store new records
  4. CHANGES    existing records compared field by field; changes go to change_log
  5. ENRICH     incomplete official records from the provider's own linked pages
  6. VERIFY     rule-based confidence score + VERIFIED / REVIEW_REQUIRED
  7. LIFECYCLE  ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE
"""
import argparse
import logging
import re
import sys
from collections import Counter

import changes
import classify
import config
import crawl
import db
import discovery
import enrich
import extract
import grounding
import lifecycle
import verify

log = logging.getLogger("main")


class QuotaExhausted(Exception):
    pass


def setup_logging(run_id: int | None = None) -> None:
    (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout)]
    if run_id:
        handlers.append(logging.FileHandler(config.DATA_DIR / "logs" / f"run_{run_id}.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers, force=True)
    for name in ("httpx", "google_genai", "pdfminer", "ddgs", "primp", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _cycle_year(s: extract.ExtractedScheme) -> int:
    """Latest year in the scheme's name or grounded dates (2025-26 -> 2026)."""
    text = " ".join(str(s.fields[f].value or "") for f in ("name", "open_date", "close_date"))
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    years += [2000 + int(y) for y in re.findall(r"\b20\d{2}\s*[-–]\s*(\d{2})\b", text)]
    return max(years, default=0)


def newest_cycle_only(schemes: list[extract.ExtractedScheme], url: str) -> list[extract.ExtractedScheme]:
    """A page may list several cycles of one scheme (2025-26 and 2026-27). They share a record
    key, so keep only the newest cycle; older cycles must not be read as a 'change'."""
    best: dict[str, extract.ExtractedScheme] = {}
    for s in schemes:
        k = extract.dedup_key(s.name, url)
        if k not in best or _cycle_year(s) > _cycle_year(best[k]):
            if k in best:
                log.info("      older cycle ignored: %s", best[k].name[:70])
            best[k] = s
        else:
            log.info("      older cycle ignored: %s", s.name[:70])
    return list(best.values())


def process_page(conn, page: crawl.Page, source_type: str, snap_id: int, run_id: int, stats: Counter) -> None:
    """Extract a new/changed official page; store new schemes, apply changes to known ones."""
    try:
        schemes = extract.extract_page(page.text, page.url, page.title, page.links)
    except extract.LLMQuotaExhausted as e:
        stats["deferred"] += 1
        raise QuotaExhausted(str(e)) from e
    except extract.LLMTemporaryError as e:  # not marked extracted -> retried next run
        stats["errors"] += 1
        log.warning("      LLM busy, page skipped for this run: %s", str(e)[:120])
        return
    db.mark_extracted(conn, page.url, page.content_hash)
    seen_ids = set()
    for s in newest_cycle_only(schemes, page.url):
        existing = db.find_by_key(conn, extract.dedup_key(s.name, page.url))
        if existing is None:
            sid = extract.store_new(conn, s, page.url, source_type, snap_id, run_id)
            stats["new"] += 1
            log.info("      NEW #%d %s", sid, s.name[:70])
            seen_ids.add(sid)
            continue
        seen_ids.add(existing["id"])
        if existing["official_url"] == page.url:
            diffs = changes.apply_changes(conn, existing["id"], s, page.url, page.text, snap_id, run_id)
            if diffs:
                stats["updated"] += 1
                for d in diffs:
                    log.info("      CHANGE DETECTED #%d %s: %s | %r -> %r", existing["id"], d["field"], d["note"],
                             d["old"], d["new"])
            else:
                stats["unchanged"] += 1
        else:  # same scheme on another page of the same provider: only fill gaps
            enrich.merge_missing(conn, existing["id"], s, page.url, snap_id, run_id)
        conn.execute("UPDATE scholarships SET last_seen_run=?, missing_runs=0 WHERE id=?", (run_id, existing["id"]))

    # records that came from this page but were not extracted this time
    for sch in conn.execute("SELECT id, name FROM scholarships WHERE official_url=?", (page.url,)).fetchall():
        if sch["id"] in seen_ids:
            continue
        name_ev = conn.execute("SELECT evidence_quote FROM field_evidence WHERE scholarship_id=? AND field='name'",
                               (sch["id"],)).fetchone()
        quote = name_ev["evidence_quote"] if name_ev else sch["name"]
        if grounding.quote_in_page(quote, page.text) >= config.FUZZY_MATCH_THRESHOLD:
            conn.execute("UPDATE scholarships SET last_seen_run=?, missing_runs=0 WHERE id=?", (run_id, sch["id"]))
        else:
            conn.execute("UPDATE scholarships SET missing_runs=missing_runs+1 WHERE id=?", (sch["id"],))
            log.info("      MISSING from its page: #%d %s", sch["id"], sch["name"][:70])
    conn.commit()


def recheck_known(conn, run_id: int, stats: Counter, llm_ok: list[bool]) -> set[str]:
    """Re-fetch every official page that has records. Unchanged pages need no LLM call."""
    urls = [r[0] for r in conn.execute("SELECT DISTINCT official_url FROM scholarships")]
    log.info("[1] RE-CHECK %d known source pages", len(urls))
    for url in urls:
        page = crawl.fetch(url)
        stats["pages"] += 1
        if not page.ok:
            conn.execute("UPDATE sources SET last_crawled=?, http_status=?, fail_count=fail_count+1 WHERE url=?",
                         (db.now_iso(), page.status, url))
            log.info("   x %s (%s)", url, page.error)
            continue
        conn.execute("UPDATE sources SET last_crawled=?, http_status=?, content_hash=?, fail_count=0 WHERE url=?",
                     (db.now_iso(), page.status, page.content_hash, url))
        snap_id, _ = crawl.record_snapshot(conn, page, run_id)
        src = db.get_source(conn, url)
        if src["extracted_hash"] == page.content_hash:
            ids = [r[0] for r in conn.execute("SELECT id FROM scholarships WHERE official_url=?", (url,))]
            conn.execute("UPDATE scholarships SET last_seen_run=?, missing_runs=0 WHERE official_url=?", (run_id, url))
            conn.execute("UPDATE field_evidence SET snapshot_id=?, run_id=? WHERE source_url=? AND grounded=1",
                         (snap_id, run_id, url))
            stats["unchanged"] += len(ids)
            log.info("   = unchanged  %s (%d records re-confirmed)", url, len(ids))
        elif llm_ok[0]:
            log.info("   * CONTENT CHANGED %s -> re-extract", url)
            try:
                process_page(conn, page, src["source_type"], snap_id, run_id, stats)
            except QuotaExhausted as e:
                llm_ok[0] = False
                log.warning("   LLM quota exhausted - remaining extraction deferred to next run (%s)", str(e)[:120])
        conn.commit()
    return set(urls)


def run(max_pages: int | None, use_search: bool) -> None:
    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn)
        conn.commit()
        setup_logging(run_id)
        log.info("=== RUN %d started ===", run_id)
        stats, llm_ok = Counter(), [True]

        rechecked = recheck_known(conn, run_id, stats, llm_ok)

        log.info("[2] DISCOVER (budget %s pages, search=%s)", max_pages or config.MAX_PAGES_PER_RUN, use_search)
        pages = discovery.discover(conn, run_id, use_search=use_search, max_pages=max_pages, skip_urls=rechecked)
        stats["pages"] += len(pages)

        todo = [rp for rp in pages if rp.changed]
        log.info("[3] EXTRACT %d new/changed official pages (%d already extracted, skipped)",
                 len(todo), len(pages) - len(todo))
        for rp in todo:
            if not llm_ok[0]:
                stats["deferred"] += 1
                continue
            log.info("   %s", rp.page.url)
            try:
                process_page(conn, rp.page, rp.source_type, rp.snapshot_id, run_id, stats)
            except QuotaExhausted as e:
                llm_ok[0] = False
                log.warning("   LLM quota exhausted - remaining pages deferred to next run (%s)", str(e)[:120])

        log.info("[4] VERIFY (pre-enrichment)")
        verify.verify_all(conn)
        if llm_ok[0]:
            log.info("[5] ENRICH incomplete official records")
            log.info("   %s", enrich.enrich_all(conn, run_id))
        log.info("[6] VERIFY")
        v = verify.verify_all(conn)
        log.info("   %s", v)
        log.info("[7] LIFECYCLE")
        lc = lifecycle.update_all(conn, run_id)
        log.info("   %s", lc)

        stale = lc.get("EXPIRED", 0) + lc.get("NO_LONGER_VERIFIABLE", 0)
        db.finish_run(conn, run_id, pages_crawled=stats["pages"], new=stats["new"], updated=stats["updated"],
                      unchanged=stats["unchanged"], stale=stale, errors=stats["errors"],
                      notes=f"deferred={stats['deferred']} verified={v['VERIFIED']} transitions={lc['transitions']}")
        log.info("=== RUN %d finished: new=%d updated=%d unchanged=%d stale=%d errors=%d deferred=%d ===",
                 run_id, stats["new"], stats["updated"], stats["unchanged"], stale, stats["errors"], stats["deferred"])
    status()


def status() -> None:
    db.init_db()
    with db.session() as conn:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        print("\n--- Scholarship Intelligence ---")
        print(f"total records       {q('SELECT COUNT(*) FROM scholarships')}")
        verified = q("SELECT COUNT(*) FROM scholarships WHERE verification_status='VERIFIED'")
        print(f"verified (>=95)     {verified}")
        print(f"average confidence  {q('SELECT ROUND(AVG(confidence),1) FROM scholarships')}")
        for r in conn.execute("SELECT status, COUNT(*) n FROM scholarships GROUP BY status ORDER BY n DESC"):
            print(f"  {r['status'] or '-':22} {r['n']}")
        print("source types        " + ", ".join(
            f"{r['provider_type']}={r['n']}" for r in
            conn.execute("SELECT provider_type, COUNT(*) n FROM scholarships GROUP BY provider_type")))
        print(f"changes logged      {q('SELECT COUNT(*) FROM change_log')}")
        print(f"crawl runs          {q('SELECT COUNT(*) FROM crawl_runs')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one full crawl cycle")
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--no-search", action="store_true", help="seeds + link following only")
    sub.add_parser("status", help="print a summary of the database")
    b = sub.add_parser("baseline", help="compare official pages with their Internet Archive copy from ~1 year ago")
    b.add_argument("--max-pages", type=int, default=12)
    args = ap.parse_args()
    if args.cmd == "run":
        run(args.max_pages, not args.no_search)
    elif args.cmd == "baseline":
        import baseline
        setup_logging()
        baseline.run_baseline(args.max_pages)
        status()
    else:
        status()
