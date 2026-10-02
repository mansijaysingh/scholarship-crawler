"""Discovery: seeds + web search + scholarship-aware link following, with a relevance gate.

Seeds are starting points, not records. Every page goes through the same path:
classify -> fetch -> relevance gate -> (if relevant) handed to extraction, and its
scholarship-looking links are queued for the next depth level.
Aggregator pages are never extracted; the scheme names they mention are turned into
new searches for the provider's own page.
"""
import logging
import re
from collections import deque
from dataclasses import dataclass

from ddgs import DDGS

import classify
import config
import crawl
import db

log = logging.getLogger("discovery")

_SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|zip|rar|docx?|xlsx?|pptx?|mp4|mp3)$", re.I)
_KW = re.compile("|".join(re.escape(k) for k in config.SCHOLARSHIP_KEYWORDS), re.I)
_DETAIL = re.compile("|".join(re.escape(k) for k in config.DETAIL_KEYWORDS), re.I)
_SCHEME_NAME = re.compile(r"\b([A-Z][\w&.'-]*(?:\s+[A-Za-z][\w&.'-]*){0,8}\s+(?:Scholarship|Fellowship)(?:\s+(?:Scheme|Programme|Program))?)\b")


@dataclass
class Candidate:
    url: str
    via: str
    depth: int


_NON_PROD_HOST = re.compile(r"^(uat|test|testing|staging|stage|demo|dev|beta|sandbox)[.\-\d]", re.I)


def is_non_production(url: str) -> bool:
    return bool(_NON_PROD_HOST.match(classify.domain_of(url)))


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_SPECIFIC = re.compile(
    r"(?:₹|rs\.?|inr)\s*[\d,]{3,}"                       # ₹ 50,000 / Rs. 12000
    r"|\b\d+(?:\.\d+)?\s*(?:lakh|lakhs|lac)\b"           # 2.5 lakh
    r"|\b\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})\b"      # 31.10.2026
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r",?\s+\d{4}"   # 31st October 2026
    r"|\b" + _MONTHS + r"\s+\d{1,2},?\s+\d{4}",          # October 31, 2026
    re.I,
)


def relevance(text: str) -> tuple[bool, int, int]:
    """A page is relevant when it describes a *specific* scholarship-like scheme:
      - scholarship keywords appear (2+ times, at least once in prose), and
      - 2+ distinct detail words (eligibility, apply, amount, last date...) in prose, and
      - at least one concrete value: a money amount, an income figure or a date.
    Prose = lines of 6+ words, so menus and link lists don't count. The concrete-value
    rule is what separates a scheme page from a portal's general 'About us' text."""
    prose = "\n".join(ln for ln in text.splitlines() if len(ln.split()) >= 6)
    kw = len(_KW.findall(text))
    kw_prose = len(_KW.findall(prose))
    detail = len(set(m.lower() for m in _DETAIL.findall(prose)))
    specific = bool(_SPECIFIC.search(text))
    return kw >= 2 and kw_prose >= 1 and detail >= 2 and specific, kw, detail


def link_looks_relevant(url: str, anchor: str) -> bool:
    if _SKIP_EXT.search(url):
        return False
    return bool(_KW.search(anchor) or _KW.search(url.replace("-", " ").replace("_", " ")))


def web_search(query: str, n: int) -> list[str]:
    try:
        return [r["href"] for r in DDGS().text(query, region="in-en", max_results=n) if r.get("href")]
    except Exception as e:  # rate limits / network: discovery degrades, run continues
        log.warning("search failed for %r: %s", query, e)
        return []


_NOT_A_NAME = re.compile(r"^(is|are|what|how|can|who|when|where|why|does|do|will|which|the|a|an|my|our|your|"
                         r"apply|check|get|top|best|list|latest|about|for|this|these|all)\b", re.I)


def scheme_names_from_aggregator(text: str, limit: int = 8) -> list[str]:
    """Scheme names mentioned on an aggregator page (used only as search terms).
    Skips sentence fragments such as FAQ questions ('Is the Aptitude Test ... Scholarship')."""
    names = []
    for m in _SCHEME_NAME.finditer(text):
        n = re.sub(r"\s+", " ", m.group(1)).strip()
        if 3 <= len(n.split()) <= 8 and not _NOT_A_NAME.match(n) and "?" not in n and n not in names:
            names.append(n)
    return names[:limit]


@dataclass
class RelevantPage:
    page: crawl.Page
    source_type: str
    discovered_via: str
    snapshot_id: int
    changed: bool  # this content has not been successfully extracted yet -> needs extraction


def discover(conn, run_id: int, extra_urls: list[str] | None = None,
             use_search: bool = True, max_pages: int | None = None,
             skip_urls: set[str] | None = None) -> list[RelevantPage]:
    """Run discovery. Returns relevant *official* pages, each snapshotted for this run.
    Every other URL seen (aggregator, unknown, irrelevant) is still recorded in `sources`.
    skip_urls: pages already fetched this run (the re-check of known records)."""
    max_pages = max_pages or config.MAX_PAGES_PER_RUN
    queue: deque[Candidate] = deque()
    seen: set[str] = {crawl.normalize_url(u) for u in (skip_urls or set())}

    def enqueue(url, via, depth):
        url = crawl.normalize_url(url)
        if url in seen or _SKIP_EXT.search(url) or is_non_production(url):
            return
        seen.add(url)
        queue.append(Candidate(url, via, depth))

    for u in config.SEED_URLS + (extra_urls or []):
        enqueue(u, "seed", 0)
    if use_search:
        for q in config.SEARCH_QUERIES:
            for u in web_search(q, config.SEARCH_RESULTS_PER_QUERY):
                enqueue(u, f"search:{q}", 0)

    relevant: list[RelevantPage] = []
    seen_hashes: dict[str, str] = {}  # content hash -> first URL with that content
    fetched = 0
    while queue and fetched < max_pages:
        c = queue.popleft()
        stype, official, _ = classify.classify(c.url)
        db.upsert_source(conn, c.url, classify.domain_of(c.url), stype, official, c.via, c.depth)

        page = crawl.fetch(c.url)
        fetched += 1
        if not page.ok:
            # keep the last good content_hash; count consecutive failures for stale detection
            conn.execute("UPDATE sources SET last_crawled=?, http_status=?, fail_count=fail_count+1 WHERE url=?",
                         (db.now_iso(), page.status, c.url))
            log.info("  x %s (%s)", c.url, page.error)
            continue
        conn.execute("UPDATE sources SET last_crawled=?, http_status=?, content_hash=?, fail_count=0 WHERE url=?",
                     (db.now_iso(), page.status, page.content_hash, c.url))
        # same content under another URL (/, /home, http vs https, old paths): keep the first
        if page.content_hash in seen_hashes:
            conn.execute("UPDATE sources SET is_relevant=0, discovered_via=discovered_via || ' [duplicate of ' || ? || ']' WHERE url=?",
                         (seen_hashes[page.content_hash], c.url))
            log.info("  = %s (duplicate of %s)", c.url, seen_hashes[page.content_hash])
            continue
        seen_hashes[page.content_hash] = c.url

        # re-classify with page text (lets foundation/CSR sites prove they name themselves)
        stype, official, reason = classify.classify(c.url, page.text)
        is_rel, kw, detail = relevance(page.text)
        conn.execute("UPDATE sources SET source_type=?, is_official=?, is_relevant=? WHERE url=?",
                     (stype, int(official), int(is_rel), c.url))
        log.info("  %s [%s] kw=%d detail=%d %s", "+" if is_rel else "-", stype, kw, detail, c.url)

        if stype == "aggregator":
            # discovery only: look for the provider's own page instead
            for name in scheme_names_from_aggregator(page.text):
                for u in web_search(f'"{name}" official apply', 5):
                    enqueue(u, f"aggregator:{c.url}|{name}", 0)
            continue

        if is_rel and official:
            snap_id, _ = crawl.record_snapshot(conn, page, run_id)
            # extract unless this exact content was already extracted successfully
            done = conn.execute("SELECT extracted_hash FROM sources WHERE url=?", (c.url,)).fetchone()
            needs = done is None or done["extracted_hash"] != page.content_hash
            relevant.append(RelevantPage(page, stype, c.via, snap_id, needs))
            log.info("      snapshot #%d (%s)", snap_id, "NEW/CHANGED -> extract" if needs else "already extracted -> skip")

        # follow scholarship-looking links on the same site
        if c.depth < config.MAX_LINK_DEPTH and (official or c.depth == 0):
            dom = classify.domain_of(c.url)
            added = 0
            for href, anchor in page.links:
                if added >= config.MAX_LINKS_PER_PAGE:
                    break
                if classify.domain_of(href) == dom and link_looks_relevant(href, anchor):
                    if href not in seen:
                        enqueue(href, f"link:{c.url}", c.depth + 1)
                        added += 1
        conn.commit()

    log.info("discovery: fetched %d pages, %d relevant official pages, %d still queued",
             fetched, len(relevant), len(queue))
    return relevant


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    max_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn)
        pages = discover(conn, run_id, max_pages=max_pages)
        db.finish_run(conn, run_id, notes="discovery test")
        print(f"\nRun {run_id} - relevant official pages:")
        for rp in pages:
            print(f"  [{rp.source_type}] snap#{rp.snapshot_id} {'CHANGED' if rp.changed else 'same   '} "
                  f"{rp.page.url}  ({len(rp.page.text)} chars)")
        print("\nSources by type:")
        for r in conn.execute("SELECT source_type, COUNT(*) n, SUM(is_relevant) rel FROM sources GROUP BY source_type"):
            print(f"  {r['source_type']:18} {r['n']:4} urls, {r['rel'] or 0} relevant")
