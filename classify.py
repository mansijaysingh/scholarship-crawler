"""Source classification: who owns this URL, and may it be treated as authoritative?

Order of rules (first match wins):
  1. aggregator / blog / news / social domains   -> aggregator   (discovery only)
  2. *.gov.in, *.nic.in, *.gov                   -> government   (official)
  3. *.ac.in, *.edu, *.edu.in                    -> university   (official)
  4. domain in KNOWN_PROVIDER_DOMAINS            -> its listed type (official)
  5. page signals: domain contains foundation/trust words, or the URL path is a
     CSR section of a company site AND the page names the organisation   -> official
  6. anything else                               -> unknown (never VERIFIED)
"""
import re
from urllib.parse import urlparse

import config

OFFICIAL_TYPES = {"government", "university", "corporate_csr", "foundation_trust"}
_FOUNDATION_WORDS = ("foundation", "trust", "charitable", "educationalsociety", "fund")


def domain_of(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url  # pages often write links as 'www.example.org/apply'
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches(domain: str, entries) -> str | None:
    for d in entries:
        if domain == d or domain.endswith("." + d):
            return d
    return None


def classify(url: str, page_text: str | None = None) -> tuple[str, bool, str]:
    """Return (source_type, is_official, reason)."""
    domain = domain_of(url)
    path = urlparse(url).path.lower()

    if _matches(domain, config.AGGREGATOR_DOMAINS):
        return "aggregator", False, f"{domain} is a known aggregator/news/social site"
    if domain.endswith(config.GOVT_SUFFIXES):
        return "government", True, f"{domain} is a government domain"
    if domain.endswith(config.UNIVERSITY_SUFFIXES):
        return "university", True, f"{domain} is an academic domain"
    known = _matches(domain, config.KNOWN_PROVIDER_DOMAINS)
    if known:
        return config.KNOWN_PROVIDER_DOMAINS[known], True, f"{domain} is a known provider domain"

    # page signals: the site must name itself, so a random blog can't claim to be a trust
    label = domain.split(".")[0]
    names_itself = bool(page_text) and _site_named_in_text(label, page_text)
    if any(w in label for w in _FOUNDATION_WORDS) and names_itself:
        return "foundation_trust", True, f"{domain} is a foundation/trust site that names itself"
    if re.search(r"/(csr|corporate-social-responsibility|sustainability)\b", path) and names_itself:
        return "corporate_csr", True, f"{domain} hosts a CSR section and names itself"
    return "unknown", False, f"{domain} could not be confirmed as an official provider"


def _site_named_in_text(label: str, text: str) -> bool:
    """True when the domain label (e.g. 'sitarambhartia') appears in the page text,
    ignoring spaces/punctuation (so 'Sitaram Bhartia' matches)."""
    squashed = re.sub(r"[^a-z0-9]", "", text[:20000].lower())
    core = re.sub(r"(foundation|trust|india|org)$", "", label) or label
    return len(core) >= 4 and core in squashed


def is_official_type(source_type: str) -> bool:
    return source_type in OFFICIAL_TYPES


if __name__ == "__main__":
    for u in [
        "https://scholarships.gov.in/",
        "https://www.iitb.ac.in/en/scholarships",
        "https://www.buddy4study.com/page/hdfc-parivartan",
        "https://www.hdfcbank.com/personal/about-us/corporate-social-responsibility/parivartan",
        "https://randomblog.example.com/top-10-scholarships",
    ]:
        print(classify(u), u)
