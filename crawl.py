"""Fetch a URL (HTML or PDF), turn it into clean text + outgoing links, hash it, snapshot it."""
import hashlib
import io
import re
import ssl
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlparse

import pdfplumber
import requests
import truststore
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

import config
import db

_HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
_session = requests.Session()
_session.headers.update(_HEADERS)


class _CompatAdapter(HTTPAdapter):
    """Fallback for government sites with old TLS setups. Certificates are STILL verified:
    it uses the OS trust store (fills in missing intermediate certs) and allows legacy
    renegotiation, which some state portals still require."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


_compat_session = requests.Session()
_compat_session.headers.update(_HEADERS)
_compat_session.mount("https://", _CompatAdapter())
_compat_hosts: set[str] = set()  # hosts that needed the fallback once


def _get(url: str, **kw) -> requests.Response:
    host = urlparse(url).netloc
    if host in _compat_hosts:
        return _compat_session.get(url, **kw)
    try:
        return _session.get(url, **kw)
    except requests.exceptions.SSLError:
        r = _compat_session.get(url, **kw)
        _compat_hosts.add(host)
        return r
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}


@dataclass
class Page:
    url: str
    status: int
    ok: bool
    text: str = ""
    title: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text)
    content_hash: str = ""
    is_pdf: bool = False
    error: str = ""


def _allowed(url: str) -> bool:
    parts = urlparse(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _get(root + "/robots.txt", timeout=10)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
                _robots[root] = rp
            else:
                _robots[root] = None  # no robots.txt -> allowed
        except requests.RequestException:
            _robots[root] = None
    rp = _robots[root]
    return rp is None or rp.can_fetch(config.USER_AGENT, url)


def _polite_wait(url: str) -> None:
    host = urlparse(url).netloc
    wait = config.POLITE_DELAY_SEC - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    return url.rstrip("/") if urlparse(url).path not in ("", "/") else url


def fetch(url: str) -> Page:
    if not _allowed(url):
        return Page(url, 0, False, error="blocked by robots.txt")
    last_err = ""
    for attempt in range(config.REQUEST_RETRIES + 1):
        _polite_wait(url)
        try:
            r = _get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"[:300]
            if isinstance(e, requests.exceptions.SSLError):
                break  # the compat fallback already failed; retrying won't help
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            return Page(url, r.status_code, False, error=f"HTTP {r.status_code}")
        ctype = r.headers.get("Content-Type", "").lower()
        if "pdf" in ctype or r.content[:5] == b"%PDF-":
            return _parse_pdf(url, r)
        if "html" in ctype or "xml" in ctype or not ctype:
            return _parse_html(url, r)
        return Page(url, r.status_code, False, error=f"unsupported content type {ctype}")
    return Page(url, 0, False, error=last_err or "fetch failed")


def _parse_html(url: str, r: requests.Response) -> Page:
    soup = BeautifulSoup(r.content, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = normalize_url(urljoin(r.url, href))
        if absu.startswith("http"):
            links.append((absu, a.get_text(" ", strip=True)[:200]))

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    # drop page chrome; fall back to whole body if that leaves nothing
    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    for tag in main.find_all(["nav", "footer", "header"]):
        tag.decompose()
    text = clean_text(main.get_text("\n", strip=True))
    return Page(url, r.status_code, True, text=text, title=title, links=links,
                content_hash=sha256(text))


def _parse_pdf(url: str, r: requests.Response) -> Page:
    try:
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages[:40]]
    except Exception as e:  # malformed PDFs are common on govt sites
        return Page(url, r.status_code, False, is_pdf=True, error=f"PDF parse error: {e}"[:300])
    text = clean_text("\n".join(parts))
    title = text.split("\n", 1)[0][:200] if text else ""
    return Page(url, r.status_code, True, text=text, title=title, is_pdf=True,
                content_hash=sha256(text))


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out, prev = [], None
    for ln in lines:
        if ln and ln != prev:
            out.append(ln)
        prev = ln
    return "\n".join(out)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_snapshot(conn, page: Page, run_id: int | None, label: str | None = None) -> int:
    """Write page text to snapshots/ (one file per distinct content hash) and record it."""
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.SNAPSHOT_DIR / f"{page.content_hash[:20]}.txt"
    if not path.exists():
        path.write_text(f"URL: {page.url}\nTITLE: {page.title}\n\n{page.text}", encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO snapshots (url, crawled_at, content_hash, text_path, run_id, label) VALUES (?,?,?,?,?,?)",
        (page.url, db.now_iso(), page.content_hash, str(path.relative_to(config.BASE_DIR)), run_id, label),
    )
    return cur.lastrowid


def record_snapshot(conn, page: Page, run_id: int) -> tuple[int, bool]:
    """Snapshot a relevant page for this run and say whether its content changed.

    Returns (snapshot_id, changed). A snapshot row is written every run, even when the
    content is identical (the text file itself is shared), so we can prove the page was
    re-checked in this run. changed=True for a new URL or a new content hash; only then
    does the page need re-extraction.
    """
    prev = conn.execute(
        "SELECT content_hash FROM snapshots WHERE url = ? ORDER BY id DESC LIMIT 1", (page.url,)
    ).fetchone()
    snap_id = save_snapshot(conn, page, run_id)
    return snap_id, prev is None or prev["content_hash"] != page.content_hash


def load_snapshot_text(text_path: str) -> str:
    raw = (config.BASE_DIR / text_path).read_text(encoding="utf-8")
    return raw.split("\n\n", 1)[1] if "\n\n" in raw else raw


if __name__ == "__main__":
    for u in ["https://www.aicte.gov.in/schemes/students-development-schemes"]:
        p = fetch(u)
        print(p.status, p.ok, p.title, len(p.text), "chars,", len(p.links), "links", p.error)
        print(p.text[:800])
