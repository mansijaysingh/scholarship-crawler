"""Central configuration: seeds, search queries, domain rules, score weights, thresholds.

Seeds are only *starting points* for discovery. Each seed is fetched, checked by the
relevance gate and expanded by link following; nothing here is a scholarship record.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "scholarships.db"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Tried in order. A model that is retired (404), overloaded (503) or out of its daily free
# quota (429 per-day) falls through to the next. Flash-Lite goes first: it has the largest
# free daily quota, and grounding checks every value in code, so a lighter model can only
# miss fields (-> Not specified), never add unsupported ones.
GEMINI_MODELS = [m for m in dict.fromkeys([
    os.getenv("GEMINI_MODEL", ""), "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-3.8-flash",
]) if m]
LLM_MIN_SECONDS_BETWEEN_CALLS = 6.0   # stay under free-tier requests-per-minute
LLM_MAX_INPUT_CHARS = 30000           # longer pages are cut down to scholarship-related windows

# ---------------------------------------------------------------- crawling
USER_AGENT = "AtlasScholarshipCrawler/0.1 (educational research project)"
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 1
POLITE_DELAY_SEC = 1.0
MAX_LINK_DEPTH = 2
MAX_PAGES_PER_RUN = 250
MAX_LINKS_PER_PAGE = 25

# ---------------------------------------------------------------- discovery
SEED_URLS = [
    # government portals / ministries / regulators
    "https://scholarships.gov.in/",
    "https://www.aicte.gov.in/schemes/students-development-schemes",
    "https://www.ugc.gov.in/",
    "https://socialjustice.gov.in/",
    "https://tribal.nic.in/",
    "https://www.education.gov.in/scholarships-education-loan-4",
    # universities
    "https://www.iitb.ac.in/",
    "https://www.du.ac.in/",
    # corporate CSR / foundations
    "https://www.hdfcbank.com/personal/about-us/corporate-social-responsibility/parivartan",
    "https://www.reliancefoundation.org/",
    "https://www.tatatrusts.org/",
    "https://www.kotakeducationfoundation.org/",
]

SEARCH_QUERIES = [
    "scholarship 2026 apply last date site:gov.in",
    "post matric scholarship 2025-26 site:gov.in",
    "merit cum means scholarship site:gov.in",
    "scholarship for students site:ac.in 2026",
    "fellowship for students India 2026 site:nic.in",
    "CSR scholarship India 2026 apply eligibility",
    "foundation scholarship India 2026 undergraduate apply",
    "girl students scholarship India 2026 apply",
    # current-cycle notices: these carry this year's dates, which verification needs
    "applications invited scholarship 2026-27 last date site:gov.in",
    "scholarship 2026-27 last date notification site:nic.in",
    "scholarship 2026-27 last date apply site:ac.in",
    "fellowship 2026-27 call for applications last date site:gov.in",
    "foundation scholarship 2026-27 last date apply eligibility India",
    "CSR scholarship 2026-27 last date apply students India",
    "state scholarship portal 2026-27 last date fresh renewal site:gov.in",
]
SEARCH_RESULTS_PER_QUERY = 10

# link-following / relevance keywords
SCHOLARSHIP_KEYWORDS = [
    "scholarship", "fellowship", "financial assistance", "merit-cum-means",
    "merit cum means", "chhatravritti", "stipend", "bursary", "free education",
]
DETAIL_KEYWORDS = [
    "eligibility", "eligible", "last date", "apply", "application", "amount",
    "income", "per annum", "deadline", "documents required",
]

# ---------------------------------------------------------------- classification
GOVT_SUFFIXES = (".gov.in", ".nic.in", ".gov")
UNIVERSITY_SUFFIXES = (".ac.in", ".edu", ".edu.in")
# discovery-only sites: never authoritative
AGGREGATOR_DOMAINS = {
    "buddy4study.com", "scholarshipsinindia.com", "vidyalakshmi.co.in",
    "shiksha.com", "collegedunia.com", "careers360.com", "jagranjosh.com",
    "indiatoday.in", "timesofindia.indiatimes.com", "ndtv.com",
    "hindustantimes.com", "wikipedia.org", "scholarshipdesk.com",
    "aglasem.com", "studyabroad.shiksha.com", "leverageedu.com",
    "medium.com", "quora.com", "reddit.com", "youtube.com", "facebook.com",
    "linkedin.com", "twitter.com", "x.com", "instagram.com",
}
# official providers that are not on .gov/.ac domains (classification still checks
# the page names the provider; this list only says what *kind* of body owns the domain)
KNOWN_PROVIDER_DOMAINS = {
    "hdfcbank.com": "corporate_csr",
    "reliancefoundation.org": "foundation_trust",
    "tatatrusts.org": "foundation_trust",
    "kotakeducationfoundation.org": "foundation_trust",
    "azimpremjifoundation.org": "foundation_trust",
    "sitarambhartia.org": "foundation_trust",
    "lic.co.in": "corporate_csr",
    "ongcindia.com": "corporate_csr",
    "sbifoundation.in": "foundation_trust",
}
# portals where applications are submitted for other providers' schemes
KNOWN_APPLICATION_PORTALS = {
    "scholarships.gov.in", "nsp.gov.in", "buddy4study.com", "vidyasaarathi.co.in",
}

# ---------------------------------------------------------------- grounding
FUZZY_MATCH_THRESHOLD = 90  # rapidfuzz partial_ratio, 0-100
NOT_SPECIFIED = "Not specified"

# ---------------------------------------------------------------- confidence score
# Points earned by evidence checks computed in code (verify.py). Sum of positives = 100.
SCORE_WEIGHTS = {
    "official_source": 25,
    "present_on_official_page": 15,
    "application_url": 10,
    "eligibility_grounded": 15,
    "deadline_grounded": 10,
    "amount_grounded": 5,
    "freshness": 10,
    "extraction_consistency": 5,
    "field_traceability": 5,
}
SCORE_PENALTIES = {
    "conflicting_official_source": -15,
    "aggregator_only": -30,
}
VERIFIED_THRESHOLD = 95.0

# ---------------------------------------------------------------- lifecycle
EXPIRING_SOON_DAYS = 15
STALE_AFTER_FAILED_RUNS = 2  # consecutive failed re-verifications before NO_LONGER_VERIFIABLE
FRESHNESS_MONTHS = 12
