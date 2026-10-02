# Demonstration: one crawler run, start to finish

This walkthrough follows the brief's demo sequence using **real output**: log lines copied from actual runs, and
queries you can re-run on the committed database (`sample_data/scholarships.db`, any SQLite browser).
Live dashboard: https://scholarship-crawler-mzwcfnrgpqpsmjbc4bbv2e.streamlit.app/

```
crawler starts → discovers → extracts → identifies official source → verifies → confidence
→ stores → displays → runs again → detects changes
```

## 1. Crawler starts

```bash
python main.py run --max-pages 20
```

```
20:58:26 === RUN 9 started ===
20:58:26 [1] RE-CHECK 94 known source pages
```

Every run first re-checks every official page that already has records, independent of search results.

## 2. Runs again: unchanged pages are re-confirmed without the LLM

Run 9 is the 6th crawl over the same database. Known pages are compared by content hash:

```
20:58:27    = unchanged  https://www.aicte.gov.in/schemes/students-development-schemes (11 records re-confirmed)
20:58:29    * CONTENT CHANGED https://www.minorityaffairs.gov.in/show_content.php?...lid=166 -> re-extract
20:58:44    = unchanged  https://www.sharda.ac.in/scholarship (13 records re-confirmed)
20:59:36    = unchanged  https://cgimunich.gov.in/.../whatsnew_Netaji_Subhas_ICAR_International_Fellowship_090926.pdf (1 records re-confirmed)
21:00:43    x https://dte.maharashtra.gov.in/merit-cum-means (HTTP 503)
...
21:11:48 === RUN 9 finished: new=0 updated=0 unchanged=178 stale=29 errors=0 deferred=0 ===
```

The Minority Affairs page changed (a news ticker), was re-extracted and compared field by field, and **no change was
logged**, because the scholarship's own facts were the same. The Maharashtra page failed (503): its failure counter
goes up, and after 2 consecutive failures its records become `NO_LONGER_VERIFIABLE`.

## 3. Discovers (not a fixed URL list)

```
21:02:26 [2] DISCOVER (budget 20 pages, search=True)
21:03:36   + [government] kw=12 detail=3 https://scholarships.gov.in/
21:03:43   + [university] kw=5 detail=5 https://www.du.ac.in/
21:03:44   - [foundation_trust] kw=1 detail=1 https://www.reliancefoundation.org/
21:03:56 discovery: fetched 20 pages, 3 relevant official pages, 127 still queued
```

`+`/`-` is the relevance gate; `[type]` is the source classification. How every URL in the database was found:

```sql
SELECT substr(discovered_via, 1, instr(discovered_via || ':', ':') - 1) AS how, COUNT(*) FROM sources GROUP BY how;
-- link 267 | search 178 | enrich 84 | seed 14 | baseline 13 | aggregator 12
```

Only 14 URLs are seeds. The rest were found by web search, link following, aggregator leads (aggregator page →
scheme name → search for the provider's page) and enrichment.

## 4. Extracts information

New pages from an earlier run (run 2):

```
13:07:39 [3] EXTRACT 63 new/changed official pages (4 already extracted, skipped)
13:07:48       NEW #50 Golden Jubilee Scholarship Scheme- 2026
13:08:13       NEW #52 Post Matric Scholarship MP, For SC, ST, and OBC Students
13:08:13       NEW #53 Vikramaditya Scholarship Scheme
```

The LLM returns `{value, evidence quotes}` per field. Code then **grounds** each value. These are real rejections
stored in the database:

```sql
SELECT scholarship_id, field, raw_value, reason FROM field_evidence WHERE grounded = 0 AND raw_value IS NOT NULL;
-- 2  | open_date | 2025-05-14 | quote found but date not present in quote
-- 16 | open_date | 2026-08-14 | quote found but date not present in quote
```

Those fields are stored as **Not specified**. Grounding also dropped scheme names that were not on the page, and
non-scholarship rows (internships, fee-table rows).

## 5. Identifies the official source, verifies, generates confidence

Record **#180 B R Samaga scholarship** (NITK's own notice). Every value traces Database → Official source → Evidence:

```sql
SELECT field, value, evidence_quote, snapshot_id FROM field_evidence WHERE scholarship_id = 180 AND grounded = 1;
```

| field | value | evidence quote (from the official PDF) |
|---|---|---|
| amount | Rs. 10,000/- | "The amount will be paid as the scholarship of Rs. 10,000/-, not in cash, but adjusted towards his / her mess b…" |
| open_date | 2026-02-25 | "Form Opening Date: 25/02/2026" |
| close_date | 2026-03-06 | "The deadline for applications is 06 March 2026 … Form Closing Date: 6/03/2026" |
| application_url | https://forms.gle/zWhuFE7cLYtKHyhn7 | "Eligible students can apply through this Google Form https://forms.gle/zWhuFE7cLYtKHyhn7" |

Its score (`score_breakdown`, shown as *Why this score?* in the dashboard):

```
+25/25 official_source           provider 'National Institute of Technology Karnataka, Surathkal' (university) runs the university site
+15/15 present_on_official_page  scheme name found in latest snapshot of the source page
 +5/10 application_url           apply link forms.gle is on another domain (half points)
+15/15 eligibility_grounded      5 eligibility fields grounded
+10/10 deadline_grounded         closing date 2026-03-06 quoted from page
 +5/5  amount_grounded           benefit amount quoted from page
+10/10 freshness                 scheme's own evidence has a current date/academic year
 +5/5  extraction_consistency    same page content re-confirmed in 4 crawl runs
 +5/5  field_traceability        12/12 extracted values traced to quotes
= 95.0 → VERIFIED   (deadline passed → status EXPIRED)
```

The same government scheme copied by a coaching site is penalised (record #148, motion.ac.in):

```
 +0/25 official_source  scheme name marks a government scheme; motion.ac.in (university) only relays it - third-party page
-30/0  aggregator_only  evidence comes only from a third-party page; official record is #48
= 40.0 → REVIEW_REQUIRED
```

## 6. Stores and displays

All tables are in `sample_data/scholarships.db`. The dashboard shows the metrics, a searchable list and, per
record: fields with their evidence quote, official and application links, status, confidence, *Why this score?*,
rejected claims, last verified and change history.

## 7. Detects changes and stale records

```sql
SELECT scholarship_id, field, old_value, new_value, note FROM change_log;
-- 135 | open_date | 2025-06-02 | 2026-07-14 | baseline: web.archive.org snapshot dated 2025-08-25
-- 129 | status | REVIEW_REQUIRED | NO_LONGER_VERIFIABLE | official page failed to load in 2 consecutive runs
-- 154 | status | REVIEW_REQUIRED | ACTIVE | verified, open until 2026-11-30
```

The Kotak Kanya change (`python main.py baseline`) compares the official page with its own Internet Archive copy.
The evidence field stores both quotes, so it can be checked at
`https://web.archive.org/web/20250825165558/https://www.kotakeducationfoundation.org/kotak-kanya-scholarship`.
28 records are `EXPIRED` (grounded deadline passed).

A first-seen-only record is tracked too:

```
MISSING from its page: #185 CBSE Single Girl Child Merit Scholarship
```

That record is counted toward `NO_LONGER_VERIFIABLE` if it is missing again.

**Note on `crawl_runs.updated`:** runs 2 and 3 show `updated` 1 and 3. These were "newly stated" fills that a later
fix proved were values the extractor had missed, not page changes: their quotes were already in the previous
snapshot. They were moved out of `change_log`, and the rule is now in code (`changes.LATE_FILL`).
