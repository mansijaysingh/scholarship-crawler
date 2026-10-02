"""Lifecycle status for every record, from evidence (first matching rule wins):

  NO_LONGER_VERIFIABLE  source page gone (HTTP 404/410), failed to load in 2 consecutive runs,
                        or the scheme was missing from its page in 2 consecutive runs
  EXPIRED               grounded closing date is in the past
  REVIEW_REQUIRED       confidence < 95 (incl. conflicts with another official source)
  EXPIRING_SOON         grounded closing date within the next 15 days
  ACTIVE                verified, and closing date in the future or not stated as closed

Status transitions are written to change_log, so stale detection has an audit trail too.
"""
from datetime import date, timedelta

import changes
import config
import db


def status_for(sch, src, today: date) -> tuple[str, str]:
    if src is not None:
        if src["http_status"] in (404, 410):
            return "NO_LONGER_VERIFIABLE", f"official page returned HTTP {src['http_status']}"
        if (src["fail_count"] or 0) >= config.STALE_AFTER_FAILED_RUNS:
            return "NO_LONGER_VERIFIABLE", f"official page failed to load in {src['fail_count']} consecutive runs"
    if (sch["missing_runs"] or 0) >= config.STALE_AFTER_FAILED_RUNS:
        return "NO_LONGER_VERIFIABLE", f"scheme not found on its official page in {sch['missing_runs']} consecutive runs"
    close = None
    try:
        close = date.fromisoformat(sch["close_date"]) if sch["close_date"] else None
    except ValueError:
        pass
    if close and close < today:
        return "EXPIRED", f"closing date {close.isoformat()} has passed"
    if sch["verification_status"] != "VERIFIED":
        return "REVIEW_REQUIRED", f"confidence {sch['confidence']} is below {config.VERIFIED_THRESHOLD}"
    if close and close <= today + timedelta(days=config.EXPIRING_SOON_DAYS):
        return "EXPIRING_SOON", f"closes on {close.isoformat()} (within {config.EXPIRING_SOON_DAYS} days)"
    return "ACTIVE", "verified" + (f", open until {close.isoformat()}" if close else ", no closing date stated")


def update_all(conn, run_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    counts: dict[str, int] = {}
    transitions = 0
    for sch in conn.execute("SELECT * FROM scholarships").fetchall():
        src = db.get_source(conn, sch["official_url"])
        status, reason = status_for(sch, src, today)
        counts[status] = counts.get(status, 0) + 1
        if status != sch["status"]:
            if sch["status"] is not None:  # first assignment is not a change
                changes.log_status_change(conn, sch["id"], sch["status"], status, run_id, reason, sch["official_url"])
                transitions += 1
            conn.execute("UPDATE scholarships SET status=? WHERE id=?", (status, sch["id"]))
    counts["transitions"] = transitions
    return counts
