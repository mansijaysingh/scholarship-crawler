"""SQLite schema and small helpers. One file, easy to inspect with any SQLite browser."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    pages_crawled   INTEGER DEFAULT 0,
    new             INTEGER DEFAULT 0,
    updated         INTEGER DEFAULT 0,
    unchanged       INTEGER DEFAULT 0,
    stale           INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    notes           TEXT
);

-- every URL the crawler has seen and how it was classified
CREATE TABLE IF NOT EXISTS sources (
    url             TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    source_type     TEXT NOT NULL,          -- government|university|corporate_csr|foundation_trust|aggregator|unknown
    is_official     INTEGER NOT NULL DEFAULT 0,
    discovered_via  TEXT,                   -- seed|search:<query>|link:<parent url>|aggregator:<url>
    depth           INTEGER DEFAULT 0,
    is_relevant     INTEGER,                -- relevance gate result (NULL = not crawled yet)
    first_seen      TEXT NOT NULL,
    last_crawled    TEXT,
    http_status     INTEGER,
    content_hash    TEXT,
    extracted_hash  TEXT,                   -- content hash last successfully extracted
    fail_count      INTEGER DEFAULT 0
);

-- raw page text per crawl, so every evidence quote can be re-checked later
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL REFERENCES sources(url),
    crawled_at      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    text_path       TEXT NOT NULL,
    run_id          INTEGER REFERENCES crawl_runs(run_id),
    label           TEXT                    -- e.g. 'archive baseline dated 2025-07-01'
);

-- one row per scholarship: the universal schema
CREATE TABLE IF NOT EXISTS scholarships (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key           TEXT UNIQUE NOT NULL,
    name                TEXT NOT NULL,
    provider            TEXT,
    provider_type       TEXT,
    official_url        TEXT,
    application_url     TEXT,
    amount              TEXT,
    education_level     TEXT,
    courses             TEXT,
    academic_req        TEXT,
    income_limit        TEXT,
    age_limit           TEXT,
    gender              TEXT,
    category            TEXT,
    domicile            TEXT,
    institution_req     TEXT,
    disability          TEXT,
    eligibility_summary TEXT,
    open_date           TEXT,
    close_date          TEXT,
    documents           TEXT,
    selection_process   TEXT,
    renewal             TEXT,
    eligibility_json    TEXT,               -- machine-readable criteria, grounded values only
    status              TEXT,               -- ACTIVE|EXPIRING_SOON|EXPIRED|REVIEW_REQUIRED|NO_LONGER_VERIFIABLE
    verification_status TEXT,               -- VERIFIED|REVIEW_REQUIRED
    confidence          REAL,
    score_breakdown     TEXT,               -- JSON list of checks, points, reasons
    first_seen          TEXT NOT NULL,
    last_verified       TEXT,
    last_changed        TEXT,
    last_seen_run       INTEGER,
    missing_runs        INTEGER DEFAULT 0   -- consecutive runs where the scheme was not found on its page
);

-- Database -> Official Source -> Evidence -> Extracted Value
CREATE TABLE IF NOT EXISTS field_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id  INTEGER NOT NULL REFERENCES scholarships(id),
    field           TEXT NOT NULL,
    value           TEXT,                   -- accepted value (NULL = Not specified)
    evidence_quote  TEXT,                   -- exact quote(s) from the page, ' … ' joined
    source_url      TEXT,
    snapshot_id     INTEGER REFERENCES snapshots(id),
    grounded        INTEGER NOT NULL DEFAULT 0,
    match_score     REAL,
    raw_value       TEXT,                   -- what the LLM claimed, kept for audit even if rejected
    reason          TEXT,                   -- why grounding accepted / rejected it
    model           TEXT,
    run_id          INTEGER,
    UNIQUE (scholarship_id, field)
);

-- old values are never lost
CREATE TABLE IF NOT EXISTS change_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id  INTEGER NOT NULL REFERENCES scholarships(id),
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    detected_at     TEXT NOT NULL,
    source_url      TEXT,
    evidence_quote  TEXT,
    run_id          INTEGER,
    note            TEXT
);

-- which linked provider pages were read to fill a record's missing fields
CREATE TABLE IF NOT EXISTS enrich_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scholarship_id  INTEGER NOT NULL REFERENCES scholarships(id),
    url             TEXT NOT NULL,
    content_hash    TEXT,
    run_id          INTEGER,
    attempted_at    TEXT NOT NULL,
    filled_fields   TEXT,                   -- JSON list
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_snap_url ON snapshots(url);
CREATE INDEX IF NOT EXISTS idx_ev_sch ON field_evidence(scholarship_id);
CREATE INDEX IF NOT EXISTS idx_chg_sch ON change_log(scholarship_id);
"""

# scholarship columns that are extracted from page text (and therefore need evidence)
EXTRACTED_FIELDS = [
    "name", "provider", "application_url", "amount", "education_level", "courses",
    "academic_req", "income_limit", "age_limit", "gender", "category", "domicile",
    "institution_req", "disability", "eligibility_summary", "open_date", "close_date",
    "documents", "selection_process", "renewal",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


MIGRATIONS = [  # (table, column, type) added after the first schema; applied to older DBs
    ("sources", "extracted_hash", "TEXT"),
]


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def mark_extracted(conn, url: str, content_hash: str) -> None:
    conn.execute("UPDATE sources SET extracted_hash = ? WHERE url = ?", (content_hash, url))


# ---------------------------------------------------------------- runs
def start_run(conn) -> int:
    cur = conn.execute("INSERT INTO crawl_runs (started_at) VALUES (?)", (now_iso(),))
    return cur.lastrowid


def finish_run(conn, run_id: int, **counts) -> None:
    cols = ", ".join(f"{k} = ?" for k in counts)
    conn.execute(
        f"UPDATE crawl_runs SET finished_at = ?{', ' + cols if cols else ''} WHERE run_id = ?",
        (now_iso(), *counts.values(), run_id),
    )


# ---------------------------------------------------------------- sources
def upsert_source(conn, url, domain, source_type, is_official, discovered_via, depth=0) -> bool:
    """Insert a newly discovered URL. Returns True if it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO sources
           (url, domain, source_type, is_official, discovered_via, depth, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (url, domain, source_type, int(is_official), discovered_via, depth, now_iso()),
    )
    return cur.rowcount == 1


def get_source(conn, url):
    return conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()


# ---------------------------------------------------------------- scholarships
def get_scholarship(conn, sch_id):
    return conn.execute("SELECT * FROM scholarships WHERE id = ?", (sch_id,)).fetchone()


def find_by_key(conn, dedup_key):
    return conn.execute("SELECT * FROM scholarships WHERE dedup_key = ?", (dedup_key,)).fetchone()


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    init_db()
    with session() as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    print(f"Database ready at {config.DB_PATH}")
    print("Tables:", ", ".join(tables))
