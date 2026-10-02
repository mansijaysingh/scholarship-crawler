# Scholarship Intelligence Crawler: technical note

Edxso AI Engineer Intern, Assignment 2 (Atlas Funding). Everything described here is implemented in this repository.

## 1. Architecture

One command, `python main.py run`, performs a full cycle. It is designed to be scheduled (cron, Windows Task Scheduler or GitHub Actions).

```
[1] RE-CHECK   every official page that already has records
[2] DISCOVER   seeds + web search + scholarship-aware link following + aggregator leads
[3] CLASSIFY   government / university / corporate_csr / foundation_trust / aggregator / unknown
[4] CRAWL      HTML or PDF -> clean text, SHA-256 hash, snapshot file per version
[5] EXTRACT    LLM returns {value, evidence quote} per field  (only for new/changed content)
[6] GROUND     code checks every quote and value against the page; unsupported -> "Not specified"
[7] CHANGES    field-by-field comparison with the stored record -> change_log
[8] ENRICH     missing fields read from the provider's own linked pages (detail pages, guideline PDFs)
[9] VERIFY     rule-based confidence score + VERIFIED / REVIEW_REQUIRED
[10] LIFECYCLE ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE
```

SQLite tables: `sources` (every URL seen, its classification, hash, failure count), `snapshots` (page text per run),
`scholarships` (universal schema, 20 extracted fields + `eligibility_json`), `field_evidence` (value, exact quote,
source URL, snapshot, grounding result and the LLM's raw claim for every field), `change_log`, `crawl_runs`, `enrich_log`.

## 2. Technology choices (all free)

| Layer | Choice | Reason |
|---|---|---|
| Fetching | requests + BeautifulSoup/lxml, pdfplumber | Government pages are mostly static HTML or PDF |
| TLS | truststore (OS trust store) | Several state portals send incomplete certificate chains; certificates are still verified |
| Search | `ddgs` (DuckDuckGo) | No API key |
| Extraction | Gemini free tier (Flash-Lite first, fallback chain) | Used only to *read*; temperature 0, JSON output |
| Matching | rapidfuzz | Quote-in-page and name matching |
| Storage / UI | SQLite, Streamlit | Inspectable single file; simple dashboard |
| Archive | Internet Archive CDX + Wayback (free) | Historical baseline for change detection |

## 3. Discovery methodology

Seeds (12 portals, ministries, universities, foundations) are only starting points. On top of them:
- **Web search**: 15 queries, including current-cycle ones such as *"applications invited scholarship 2026-27 last date site:gov.in"*.
- **Link following** to depth 2 on the same site, only for links whose anchor text or URL contains scholarship vocabulary.
- **Aggregator leads**: aggregator pages are never extracted. Scheme names found on them become new searches for the provider's own page.
- **Relevance gate** (code): scholarship words appear at least twice, at least once in prose; at least 2 distinct detail words (eligibility, apply, amount…); and at least one concrete value (₹ amount, lakh figure or date). This removes portal "About us" pages that only mention scholarships in menus.
- Duplicate content (same hash under several URLs) and non-production hosts (`uat.`, `test.`…) are skipped.

## 4. Extraction methodology

The LLM receives one page (or keyword-centred windows of long pages) and must return, per scheme and per field, `{"value", "evidence": [1-4 exact quotes]}`. The rules are: page text only; the page's own wording; `null` when the page does not say it. Several schemes on one page become separate records. When one page lists several cycles of the same scheme, only the newest is kept. Records whose name has no funding term (scholarship, fellowship, financial assistance, freeship, grant…) are dropped, which removes fee-table rows and internships.

## 5. Anti-hallucination approach (grounding)

Every extracted value is checked **in code** before storage:
1. **Quote check:** every evidence quote must occur in the page (exact after normalisation, or rapidfuzz partial match ≥ 90).
2. **Value check:** the value must be stated inside the quotes. Dates must parse to the same day (Indian day-first formats); every number in the value must appear in the quotes (with lakh/crore expansion); gender must match a term; names must fuzzy-match; free text must share ≥ 60 % of its content words; application URLs must be written on, or linked from, the page.
3. Failure on either check → **"Not specified"**, with `grounded = 0`. The LLM's rejected claim and the reason are kept in `field_evidence` for audit.
4. Structured eligibility (`max_family_income_inr`, categories, levels…) is kept only if supported by that field's grounded quote.

So every stored value traces **Database → Official source URL → snapshot → exact quote → value**.

## 6. Verification and confidence-score methodology

The score is the sum of fixed, evidence-based checks (weights in `config.py`). No LLM produces or adjusts a number.

| Check | Points | Rule |
|---|---|---|
| Official source | 25 | Source domain is official **and belongs to the scheme's provider** |
| Present on official page now | 15 | Name grounded in the latest snapshot; page loaded in the latest crawl |
| Application URL | 10 | Grounded link on the provider's domain or a known portal (5 if elsewhere) |
| Eligibility grounded | 15 | Scaled: 3+ grounded eligibility fields = full points |
| Deadline grounded | 10 | Closing date quoted, or page states rolling admission |
| Amount grounded | 5 | Benefit quoted |
| Freshness | 10 | Scheme evidence has a current date/academic year (5 if only the page does; 0 if the scheme is named for an old year) |
| Extraction consistency | 5 | Same scheme corroborated by another record, or identical content confirmed in 2+ runs |
| Field traceability | 5 | Share of extracted values that grounded |
| Conflicting official source | −15 | Another provider-owned source disagrees on deadline/amount/income |
| Third-party only | −30 | Evidence only from a page that is not the provider's |

**Provider ownership** is the key authenticity rule. Government domains are official only for government schemes, and university domains only for that university's own scholarships. A university or coaching site relaying a *National / Central Sector / Post-Matric…* scheme, or a blog/news/listicle page, is a third-party page even on an `.ac.in` domain. **VERIFIED** requires ≥ 95; everything else is **REVIEW_REQUIRED**. Each check's points and reason are stored in `score_breakdown` and shown as *"Why this score?"*.

## 7. Change and stale detection

- **Run to run:** unchanged content hash → the record is re-confirmed with no LLM call. A changed hash → re-extract and compare field by field. A change is logged only when the **evidence** changed: dates differ, or for numbers and text the old quote is no longer on the page. A field that "disappears" while its old quote is still on the page is an extractor miss, not a change. A value the extractor missed last time is filled silently, because its quote was already in the previous snapshot. Each change stores old value, new value, date, source and evidence; old values are never lost.
- **Archive baseline** (`python main.py baseline`): today's official page is compared with its web.archive.org copy from about a year earlier, using the same extraction and rules. These changes are labelled *"baseline: web.archive.org snapshot dated …"* with both quotes.
- **Lifecycle:** NO_LONGER_VERIFIABLE (HTTP 404/410, 2 consecutive failed fetches, or scheme missing from its page in 2 runs) → EXPIRED (grounded deadline passed) → REVIEW_REQUIRED (< 95) → EXPIRING_SOON (≤ 15 days) → ACTIVE. Status transitions are logged.

## 8. Results and limitations

See README → *Results* for current counts. Known limitations: JavaScript-only portals (e.g. some application portals) yield little text without a headless browser; many scheme pages are "evergreen" and state no deadline or amount, which honestly caps their confidence below 95; the Gemini free tier (≈20 requests/day for larger models) limits extraction volume per day; the remaining work carries over to the next run automatically.
