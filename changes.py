"""Change detection: compare a fresh extraction with the stored record, field by field.

The LLM may word the same fact differently between runs, so a change is only logged
when the EVIDENCE changed, not merely the wording:
  - dates:            the normalised date differs
  - numeric fields:   the numbers differ AND the old quote is no longer on the page
  - text fields:      the value differs AND the old quote is no longer on the page
  - a field vanishes: only if its old quote is really gone from the page
                      (otherwise the extractor just missed it; the old value is kept)
Every change writes a change_log row (old, new, date, source, evidence). The old value
is never lost: it stays in change_log, and the scholarship row gets the new value.
"""
from rapidfuzz import fuzz

import config
import crawl
import db
import grounding
from extract import ExtractedScheme

DATE_FIELDS = {"open_date", "close_date"}
NUMERIC_FIELDS = {"amount", "income_limit", "age_limit", "academic_req"}
SKIP_FIELDS = {"name"}  # identity of the record (dedup key), not compared


def _old_quote_still_on_page(old_quote: str | None, page_text: str) -> bool:
    if not old_quote:
        return False
    quotes = old_quote.split(grounding.QUOTE_JOIN)
    return all(grounding.quote_in_page(q, page_text) >= config.FUZZY_MATCH_THRESHOLD for q in quotes)


LATE_FILL = "LATE_FILL"  # value was on the page before too; the extractor missed it last time


def diff_field(field: str, old: dict | None, new, page_text: str, prev_text: str | None = None) -> str | None:
    """Return a note if this field really changed, LATE_FILL for a value the extractor
    missed before, else None."""
    old_val = old["value"] if old and old["grounded"] else None
    new_val = new.value if new.grounded else None
    if old_val is None and new_val is None:
        return None
    if old_val is None:
        # only a change if the page did NOT already state it in the previous snapshot
        if prev_text and _old_quote_still_on_page(new.evidence, prev_text):
            return LATE_FILL
        return "newly stated on the official page"
    still_there = _old_quote_still_on_page(old["evidence_quote"], page_text)
    if new_val is None:
        return None if still_there else "no longer stated on the official page"
    if field in DATE_FIELDS:
        return "date changed" if old_val != new_val else None
    if still_there:
        return None  # old evidence still on the page: same fact, at most re-worded
    if field in NUMERIC_FIELDS:
        same = grounding.numbers_in(old_val) == grounding.numbers_in(new_val)
        return None if same else "value changed"
    same = fuzz.token_set_ratio(grounding.norm(old_val), grounding.norm(new_val)) >= 90
    return None if same else "value changed"


def apply_changes(conn, sch_id: int, scheme: ExtractedScheme, url: str, page_text: str,
                  snapshot_id: int, run_id: int) -> list[dict]:
    """Compare and update one record from its own official page. Returns the logged changes."""
    old_ev = {r["field"]: r for r in conn.execute("SELECT * FROM field_evidence WHERE scholarship_id=?", (sch_id,))}
    prev = conn.execute("SELECT text_path FROM snapshots WHERE url=? AND id<? ORDER BY id DESC LIMIT 1",
                        (url, snapshot_id)).fetchone()
    try:
        prev_text = crawl.load_snapshot_text(prev["text_path"]) if prev else None
    except FileNotFoundError:
        prev_text = None
    now = db.now_iso()
    changes = []
    for f, g in scheme.fields.items():
        if f in SKIP_FIELDS:
            continue
        note = diff_field(f, old_ev.get(f), g, page_text, prev_text)
        old = old_ev.get(f)
        if note == LATE_FILL:  # fill silently: not a change on the official page
            conn.execute(f"UPDATE scholarships SET {f}=? WHERE id=?", (g.value, sch_id))
            _write_evidence(conn, sch_id, f, g, url, snapshot_id, run_id, scheme.model,
                            f"filled late (stated on the page before, missed by the extractor): {g.reason}")
            continue
        if not note:
            # same fact, but if the old quote is gone, point the evidence at the new quote
            if g.grounded and old and old["grounded"] and old["source_url"] == url \
                    and not _old_quote_still_on_page(old["evidence_quote"], page_text):
                conn.execute("UPDATE field_evidence SET evidence_quote=?, snapshot_id=?, match_score=?, run_id=? "
                             "WHERE scholarship_id=? AND field=?",
                             (g.evidence, snapshot_id, g.match_score, run_id, sch_id, f))
            continue
        old_val = old["value"] if old and old["grounded"] else None
        new_val = g.value if g.grounded else None
        if old and old["source_url"] != url and old_val is not None and new_val is None:
            continue  # value came from another provider page (enrichment); this page never stated it
        conn.execute(
            """INSERT INTO change_log (scholarship_id, field, old_value, new_value, detected_at, source_url,
                   evidence_quote, run_id, note) VALUES (?,?,?,?,?,?,?,?,?)""",
            (sch_id, f, old_val, new_val, now, url, g.evidence if new_val else (old["evidence_quote"] if old else None),
             run_id, note))
        conn.execute(f"UPDATE scholarships SET {f}=?, last_changed=? WHERE id=?", (new_val, now, sch_id))
        _write_evidence(conn, sch_id, f, g, url, snapshot_id, run_id, scheme.model, f"{note}: {g.reason}")
        changes.append({"field": f, "old": old_val, "new": new_val, "note": note})
    # fields whose old evidence is still on the page are re-confirmed by this snapshot
    conn.execute("UPDATE field_evidence SET snapshot_id=?, run_id=? WHERE scholarship_id=? AND source_url=? AND grounded=1",
                 (snapshot_id, run_id, sch_id, url))
    return changes


def _write_evidence(conn, sch_id, f, g, url, snapshot_id, run_id, model, reason) -> None:
    conn.execute(
        """INSERT INTO field_evidence (scholarship_id, field, value, evidence_quote, source_url, snapshot_id,
               grounded, match_score, raw_value, reason, model, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(scholarship_id, field) DO UPDATE SET value=excluded.value,
               evidence_quote=excluded.evidence_quote, source_url=excluded.source_url,
               snapshot_id=excluded.snapshot_id, grounded=excluded.grounded, match_score=excluded.match_score,
               raw_value=excluded.raw_value, reason=excluded.reason, model=excluded.model, run_id=excluded.run_id""",
        (sch_id, f, g.value if g.grounded else None, g.evidence if g.grounded else None, url, snapshot_id,
         int(g.grounded), g.match_score, g.raw_value, reason, model, run_id))


def log_status_change(conn, sch_id: int, old: str | None, new: str, run_id: int, reason: str, url: str) -> None:
    conn.execute(
        """INSERT INTO change_log (scholarship_id, field, old_value, new_value, detected_at, source_url,
               evidence_quote, run_id, note) VALUES (?,?,?,?,?,?,?,?,?)""",
        (sch_id, "status", old, new, db.now_iso(), url, None, run_id, reason))
