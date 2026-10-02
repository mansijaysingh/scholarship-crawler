# Scholarship Intelligence Crawler

A working auto-crawler that discovers scholarships for Indian students, extracts them into one schema, traces every
important field to an exact quote on the **official** source, scores confidence with a **rule-based** method, stores
everything in SQLite, re-runs to detect **changes** and **stale/expired** records, and shows the result in a dashboard.

Built for the Edxso AI Engineer Intern assignment 2 (Atlas Funding). Method details: [TECHNICAL_NOTE.md](TECHNICAL_NOTE.md).

> **Principle:** accuracy over quantity. If the official page does not say it, the database says *Not specified*.

## Results

<!-- RESULTS:START -->
_Filled in from the database after the latest run._
<!-- RESULTS:END -->

## How it works (one run)

```
RE-CHECK known official pages → DISCOVER (seeds + search + link following) → CLASSIFY source
 → CRAWL (HTML/PDF, hash, snapshot) → EXTRACT (LLM: value + exact quote) → GROUND (code checks quote & value)
 → CHANGES (field-by-field vs stored record) → ENRICH (provider's linked pages) → VERIFY (rule-based score)
 → LIFECYCLE (ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE)
```

- The LLM (Gemini free tier) is used **only to read**. It returns `{value, evidence quotes}`. Code accepts a value only if
  the quotes are on the page **and** the value is inside them (same date, same numbers, matching terms).
- The **confidence score** is a sum of fixed evidence checks (official provider-owned source 25, present now 15,
  application URL 10, eligibility 15, deadline 10, amount 5, freshness 10, consistency 5, traceability 5,
  conflict −15, third-party −30). **VERIFIED ≥ 95**, otherwise **REVIEW_REQUIRED**. No LLM-generated numbers.
- **Official source** means the page belongs to the organisation that runs the scheme. A university, coaching or blog
  page describing a government scheme is counted as a third-party page, even on an `.ac.in` domain.

## Setup

Requires Python 3.11+ (tested on 3.12, Windows 11).

```bash
git clone <repo-url> scholarship-crawler
cd scholarship-crawler
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # Linux/macOS: cp .env.example .env
```

Put a free Gemini API key (https://aistudio.google.com/apikey) in `.env` as `GEMINI_API_KEY=...`.
The key is only needed for crawling; the dashboard works without it.

## Usage

```bash
python main.py run                  # one full crawl cycle (re-check + discover + extract + verify + lifecycle)
python main.py run --max-pages 80   # smaller discovery budget
python main.py baseline             # change detection against web.archive.org copies of official pages
python main.py status               # summary of the database
python export.py                    # refresh sample_data/ (DB copy, CSV, JSON)
streamlit run dashboard.py          # dashboard at http://localhost:8501
```

The dashboard reads `data/scholarships.db` (your local crawler output) or, if that does not exist, the committed
`sample_data/scholarships.db`.

### Running it automatically

The crawler is built to run repeatedly. Each run re-checks known pages, skips the LLM for unchanged content, and
continues pages deferred by the free-tier quota.

- **Windows Task Scheduler** (daily at 06:00):
  `schtasks /create /tn ScholarshipCrawler /sc daily /st 06:00 /tr "cmd /c cd /d C:\path\to\scholarship-crawler && .venv\Scripts\python main.py run"`
- **cron** (Linux/macOS, daily at 06:00):
  `0 6 * * * cd /path/to/scholarship-crawler && .venv/bin/python main.py run >> data/cron.log 2>&1`

## Inspecting the data

- `sample_data/scholarships.db`: open with any SQLite browser. Main tables: `scholarships`, `field_evidence`
  (value + exact quote + source URL + snapshot for every field), `change_log`, `crawl_runs`, `sources`, `snapshots`.
- `sample_data/scholarships.csv` / `.json`: same records. The JSON includes per-field evidence, score breakdown and change history.

Trace any value, for example a deadline:

```sql
SELECT s.name, fe.value, fe.evidence_quote, fe.source_url, fe.snapshot_id
FROM field_evidence fe JOIN scholarships s ON s.id = fe.scholarship_id
WHERE fe.field = 'close_date' AND fe.grounded = 1;
```

## Repository layout

| File | Purpose |
|---|---|
| `main.py` | CLI, one full crawl cycle |
| `config.py` | seeds, search queries, domain rules, score weights, thresholds |
| `db.py` | SQLite schema and helpers |
| `discovery.py` | search, link following, relevance gate, aggregator leads |
| `classify.py` | source-type classification |
| `crawl.py` | fetch HTML/PDF, clean text, hash, snapshots |
| `extract.py` | LLM extraction (value + evidence), model fallback, non-scholarship filter |
| `grounding.py` | quote-in-page and value-in-quote checks (anti-hallucination) |
| `enrich.py` | fill missing fields from the provider's own linked pages |
| `verify.py` | confidence score, provider-ownership rules, "why this score" |
| `changes.py` | change detection (evidence-based, no silent overwrite) |
| `lifecycle.py` | ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE |
| `baseline.py` | archive-baseline change detection (web.archive.org) |
| `dashboard.py` | Streamlit UI |
| `export.py` | writes `sample_data/` |

## Honest notes and limitations

- **Free tools only:** requests/BeautifulSoup/pdfplumber, DuckDuckGo search (`ddgs`), Gemini **free tier**, SQLite,
  Streamlit, Internet Archive. No paid APIs or scraping services.
- The Gemini free tier allows about 20 requests/day for the larger models, so Flash-Lite is used first. Grounding makes
  a lighter model safe: it can miss a field (→ *Not specified*) but cannot add an unsupported one.
- Many official scheme pages are "evergreen" (rules without this year's deadline or amount). Those records honestly
  stay below 95 rather than being filled from other sites.
- JavaScript-only pages (some application portals) yield little text without a headless browser.
- Change examples are only what the crawler actually observed: run-to-run, or against a labelled archive copy.
  Nothing in the database is edited by hand.
