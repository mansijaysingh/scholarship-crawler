"""Scholarship Intelligence dashboard (Streamlit).

    streamlit run dashboard.py

Reads the crawler's SQLite database read-only. Uses data/scholarships.db when present
(local crawler output), otherwise the committed copy in sample_data/ (for the hosted demo).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import config

STATUS_ORDER = ["ACTIVE", "EXPIRING_SOON", "EXPIRED", "REVIEW_REQUIRED", "NO_LONGER_VERIFIABLE"]
FIELD_LABELS = {
    "name": "Name", "provider": "Provider", "application_url": "Application URL", "amount": "Amount / benefit",
    "education_level": "Education level", "courses": "Courses", "academic_req": "Academic requirement",
    "income_limit": "Income limit", "age_limit": "Age limit", "gender": "Gender", "category": "Category",
    "domicile": "Domicile / state", "institution_req": "Institution requirement", "disability": "Disability",
    "eligibility_summary": "Eligibility", "open_date": "Opening date", "close_date": "Closing date",
    "documents": "Documents required", "selection_process": "Selection process", "renewal": "Renewal",
}

st.set_page_config(page_title="Scholarship Intelligence", page_icon="🎓", layout="wide")


def db_path():
    if config.DB_PATH.exists():
        return config.DB_PATH
    return config.BASE_DIR / "sample_data" / "scholarships.db"


@st.cache_data(ttl=60)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def ns(v) -> str:
    return config.NOT_SPECIFIED if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else str(v)


def fmt_date(iso) -> str:
    if not iso or (isinstance(iso, float) and pd.isna(iso)):
        return config.NOT_SPECIFIED
    try:
        return datetime.fromisoformat(str(iso)[:19]).strftime("%d %b %Y")
    except ValueError:
        return str(iso)


# ---------------------------------------------------------------- data
sch = q("""SELECT s.*, src.source_type, src.is_official, src.http_status
           FROM scholarships s LEFT JOIN sources src ON src.url = s.official_url""")
if sch.empty:
    st.warning(f"No records yet in {db_path()}. Run `python main.py run` first.")
    st.stop()
sch["owns_source"] = sch["score_breakdown"].apply(lambda b: bool(json.loads(b or "{}").get("owns_source")))
week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
recent = sch[sch["last_changed"].fillna("") >= week_ago]  # a field changed on the official page

# ---------------------------------------------------------------- header + metrics
st.title("🎓 Scholarship Intelligence")
runs = q("SELECT * FROM crawl_runs ORDER BY run_id DESC")
st.caption(f"Database: `{db_path().name}` · {len(runs)} crawl runs · last run finished "
           f"{fmt_date(runs['finished_at'].dropna().iloc[0]) if runs['finished_at'].notna().any() else '-'} · "
           f"VERIFIED requires confidence ≥ {config.VERIFIED_THRESHOLD:.0f}%")

m = st.columns(8)
m[0].metric("Total discovered", len(sch))
m[1].metric("Verified (≥95%)", int((sch["verification_status"] == "VERIFIED").sum()))
m[2].metric("Review required", int((sch["status"] == "REVIEW_REQUIRED").sum()))
m[3].metric("Active", int(sch["status"].isin(["ACTIVE", "EXPIRING_SOON"]).sum()))
m[4].metric("Expired", int((sch["status"] == "EXPIRED").sum()))
m[5].metric("No longer verifiable", int((sch["status"] == "NO_LONGER_VERIFIABLE").sum()))
m[6].metric("Recently updated (7d)", len(recent))
m[7].metric("Average confidence", f"{sch['confidence'].mean():.1f}%")

tab_list, tab_changes, tab_runs, tab_method = st.tabs(["Scholarships", "Change history", "Crawl runs", "Methodology"])

# ---------------------------------------------------------------- scholarships
with tab_list:
    f1, f2, f3, f4 = st.columns([3, 2, 2, 1.4])
    search = f1.text_input("Search name / provider / eligibility", "")
    status_f = f2.multiselect("Status", STATUS_ORDER, default=[])
    types = sorted(sch["source_type"].dropna().unique())
    type_f = f3.multiselect("Source type", types, default=[])
    own_only = f4.checkbox("Official source only", value=False,
                           help="Only records whose source page belongs to the scheme's provider")

    view = sch.copy()
    if search:
        s = search.lower()
        view = view[view[["name", "provider", "eligibility_summary"]].fillna("").apply(
            lambda r: s in " ".join(r).lower(), axis=1)]
    if status_f:
        view = view[view["status"].isin(status_f)]
    if type_f:
        view = view[view["source_type"].isin(type_f)]
    if own_only:
        view = view[view["owns_source"]]
    view = view.sort_values(["confidence", "name"], ascending=[False, True])

    table = pd.DataFrame({
        "ID": view["id"], "Scholarship": view["name"], "Provider": view["provider"].map(ns),
        "Source type": view["source_type"], "Official source": view["owns_source"],
        "Amount": view["amount"].map(ns), "Deadline": view["close_date"].map(fmt_date),
        "Status": view["status"], "Confidence": view["confidence"],
    })
    st.caption(f"{len(table)} of {len(sch)} records · click a row to open its details")
    event = st.dataframe(
        table, hide_index=True, use_container_width=True, height=380,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%.1f%%"),
            "Official source": st.column_config.CheckboxColumn("Official source"),
        },
    )
    rows = event.selection.rows if event and event.selection else []
    sel_id = int(table.iloc[rows[0]]["ID"]) if rows else (int(table.iloc[0]["ID"]) if len(table) else None)

    if sel_id is not None:
        r = sch[sch["id"] == sel_id].iloc[0]
        bd = json.loads(r["score_breakdown"] or "{}")
        st.divider()
        st.subheader(f"Scholarship details · #{sel_id}")
        st.markdown(f"### {r['name']}")
        badge = {"ACTIVE": "🟢", "EXPIRING_SOON": "🟠", "EXPIRED": "⚫", "REVIEW_REQUIRED": "🟡",
                 "NO_LONGER_VERIFIABLE": "🔴"}.get(r["status"], "⚪")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Confidence", f"{r['confidence']:.1f}%")
        c2.metric("Verification", r["verification_status"] or "-")
        c3.metric("Status", f"{badge} {r['status']}")
        c4.metric("Last verified", fmt_date(r["last_verified"]))

        d1, d2 = st.columns(2)
        with d1:
            st.markdown(f"**Provider:** {ns(r['provider'])}")
            st.markdown(f"**Amount / benefit:** {ns(r['amount'])}")
            st.markdown(f"**Deadline:** {fmt_date(r['close_date'])}")
            st.markdown(f"**Eligibility:** {ns(r['eligibility_summary'])}")
        with d2:
            st.markdown(f"**Official source:** [{r['official_url'][:70]}]({r['official_url']})  \n"
                        f"source type `{r['source_type']}` · "
                        f"{'✅ provider’s own site' if r['owns_source'] else '⚠️ third-party page'}")
            app = r["application_url"]
            if app and not pd.isna(app):
                link = app if "://" in app else "https://" + app
                st.markdown(f"**Application link:** [{app}]({link})")
            else:
                st.markdown(f"**Application link:** {config.NOT_SPECIFIED}")
            st.markdown(f"**First seen:** {fmt_date(r['first_seen'])} · **Last changed:** {fmt_date(r['last_changed'])}")
            elig = json.loads(r["eligibility_json"] or "{}")
            if elig:
                st.markdown("**Machine-readable eligibility:**")
                st.json(elig, expanded=False)

        st.markdown("#### Why this score?")
        checks = pd.DataFrame(bd.get("checks", []))
        if not checks.empty:
            checks = checks.rename(columns={"check": "Check", "points": "Points", "max": "Max", "reason": "Evidence / reason"})
            st.dataframe(checks[["Check", "Points", "Max", "Evidence / reason"]], hide_index=True,
                         use_container_width=True)
            st.caption(f"Sum of checks = {r['confidence']:.1f}% (capped 0–100). Every check is computed in code "
                       f"from stored evidence; no model generates or adjusts the number.")

        st.markdown("#### Source evidence (Database → Official source → Evidence → Value)")
        ev = q("""SELECT fe.field, fe.value, fe.grounded, fe.evidence_quote, fe.source_url, fe.snapshot_id,
                         fe.raw_value, fe.reason, sn.crawled_at
                  FROM field_evidence fe LEFT JOIN snapshots sn ON sn.id = fe.snapshot_id
                  WHERE fe.scholarship_id = ?""", (sel_id,))
        if not ev.empty:
            ev["order"] = ev["field"].map({k: i for i, k in enumerate(FIELD_LABELS)})
            ev = ev.sort_values("order")
            show = pd.DataFrame({
                "Field": ev["field"].map(FIELD_LABELS),
                "Value": ev.apply(lambda e: e["value"] if e["grounded"] else config.NOT_SPECIFIED, axis=1),
                "Evidence quote (from the page)": ev["evidence_quote"].fillna(""),
                "Source page": ev["source_url"].fillna(""),
                "Snapshot": ev["snapshot_id"].apply(lambda x: f"#{int(x)}" if pd.notna(x) else ""),
                "Grounding": ev["reason"].fillna(""),
            })
            st.dataframe(show, hide_index=True, use_container_width=True,
                         column_config={"Source page": st.column_config.LinkColumn("Source page")})
            rejected = ev[(ev["grounded"] == 0) & ev["raw_value"].notna()]
            if not rejected.empty:
                with st.expander(f"Values the extractor proposed but grounding rejected ({len(rejected)})"):
                    st.dataframe(pd.DataFrame({"Field": rejected["field"], "Proposed": rejected["raw_value"],
                                               "Why rejected": rejected["reason"]}),
                                 hide_index=True, use_container_width=True)

        st.markdown("#### Change history")
        ch = q("""SELECT detected_at, field, old_value, new_value, note, source_url, evidence_quote, run_id
                  FROM change_log WHERE scholarship_id = ? ORDER BY id DESC""", (sel_id,))
        if ch.empty:
            st.caption("No changes detected for this scholarship.")
        else:
            ch["detected_at"] = ch["detected_at"].map(fmt_date)
            st.dataframe(ch, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- change log
with tab_changes:
    st.subheader("All detected changes")
    st.caption("Old values are never overwritten silently: each change keeps old value, new value, date, source "
               "and evidence. Status transitions (e.g. ACTIVE → EXPIRED) are logged here too.")
    allch = q("""SELECT c.detected_at, c.scholarship_id AS id, s.name, c.field, c.old_value, c.new_value,
                        c.note, c.source_url, c.evidence_quote, c.run_id
                 FROM change_log c JOIN scholarships s ON s.id = c.scholarship_id ORDER BY c.id DESC""")
    if allch.empty:
        st.info("No changes detected yet.")
    else:
        allch["detected_at"] = allch["detected_at"].map(fmt_date)
        st.dataframe(allch, hide_index=True, use_container_width=True,
                     column_config={"source_url": st.column_config.LinkColumn("source_url")})
    st.subheader("Stale / expired records")
    stale = sch[sch["status"].isin(["EXPIRED", "NO_LONGER_VERIFIABLE"])]
    st.dataframe(pd.DataFrame({"ID": stale["id"], "Scholarship": stale["name"], "Status": stale["status"],
                               "Closing date": stale["close_date"].map(fmt_date),
                               "Source HTTP status": stale["http_status"], "Missing runs": stale["missing_runs"],
                               "Official source": stale["official_url"]}),
                 hide_index=True, use_container_width=True,
                 column_config={"Official source": st.column_config.LinkColumn("Official source")})

# ---------------------------------------------------------------- runs
with tab_runs:
    st.subheader("Crawl runs")
    st.caption("Each run re-checks known official pages, discovers new ones, extracts, verifies and updates statuses.")
    st.dataframe(runs, hide_index=True, use_container_width=True)
    src = q("""SELECT source_type, COUNT(*) AS urls, SUM(is_relevant) AS relevant, SUM(is_official) AS official
               FROM sources GROUP BY source_type ORDER BY urls DESC""")
    st.subheader("Sources seen by the crawler")
    st.dataframe(src, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- methodology
with tab_method:
    st.subheader("How the confidence score is computed")
    rows = [{"Check": k, "Points": v} for k, v in config.SCORE_WEIGHTS.items()] + \
           [{"Check": k + " (penalty)", "Points": v} for k, v in config.SCORE_PENALTIES.items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.markdown(f"""
- **VERIFIED** only when confidence ≥ {config.VERIFIED_THRESHOLD:.0f}%; otherwise **REVIEW_REQUIRED**.
- **Official source** = the page's site belongs to the organisation that runs the scheme. A university or
  coaching site describing a government scheme, or a blog/listicle, is a *third-party page* (−30).
- **Grounding:** the LLM only reads pages and returns `{{value, evidence quote}}` per field. Code checks that
  the quote is on the page (exact or ≥{config.FUZZY_MATCH_THRESHOLD}% fuzzy match) **and** that the value is
  inside the quote (same date, every number present, matching terms). Otherwise the field is *Not specified*.
- **Change detection:** a change is logged only when the page's evidence changed, not when the extractor
  re-worded a value. Archive baselines compare today's official page with its web.archive.org copy.
""")
