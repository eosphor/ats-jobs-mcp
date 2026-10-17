"""Auto-detect a company's ATS from its website or careers-page URL.

Strategy (cheap first, all public, no login):
  1. If the input already IS an ATS URL (boards.greenhouse.io/x, jobs.lever.co/x,
     x.wd5.myworkdayjobs.com/Site ...) parse it directly - zero requests.
  2. Otherwise fetch the page (and /careers, /jobs for bare domains) and look
     for embedded ATS links or widgets.
  3. Last resort: probe the public Greenhouse / Lever / Ashby APIs with the
     domain stem as the handle (e.g. stripe.com -> "stripe").
Every input yields either a source config or an explicit "not detected" reason.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("recruitee", re.compile(r"([A-Za-z0-9-]+)\.recruitee\.com", re.I)),
    ("personio", re.compile(r"([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("teamtailor", re.compile(r"([A-Za-z0-9-]+)\.teamtailor\.com", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"([A-Za-z0-9-]+)\.workable\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("breezy", re.compile(r"([A-Za-z0-9-]+)\.breezy\.hr", re.I)),
]
_WORKDAY = re.compile(
    r"([A-Za-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I
)
_IGNORE_HANDLES = {"embed", "api", "www", "jobs", "careers", "static", "assets", "cdn", "app", "v1", "js", "apply"}


def _normalize(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def _stem(url: str) -> Optional[str]:
    host = (urlparse(url).hostname or "").lower()
    parts = [p for p in host.split(".") if p and p not in ("www", "careers", "jobs")]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else None


def find_in_text(text: str) -> Optional[Dict[str, str]]:
    """Return the first ATS source config found in a URL or HTML text."""
    m = _WORKDAY.search(text)
    if m:
        tenant, wd, site = m.group(1), m.group(2), m.group(3)
        if site.lower() not in _IGNORE_HANDLES:
            return {"vendor": "workday", "handle": tenant.lower(), "tenant": tenant,
                    "host": f"{tenant}.{wd}.myworkdayjobs.com".lower(), "site": site}
    for vendor, pat in _PATTERNS:
        for m in pat.finditer(text):
            handle = m.group(1).strip("/.")
            if handle and handle.lower() not in _IGNORE_HANDLES:
                return {"vendor": vendor, "handle": handle}
    return None


async def _probe(http, vendor: str, handle: str) -> bool:
    try:
        if vendor == "workable":
            data = await http.post_json(
                f"https://apply.workable.com/api/v3/accounts/{handle}/jobs", json_body={}
            )
            return isinstance(data, dict) and bool(data.get("results"))
        if vendor == "smartrecruiters":
            data = await http.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{handle}/postings",
                params={"limit": 1},
            )
            return isinstance(data, dict) and bool(data.get("content"))
        if vendor == "breezy":
            data = await http.get_json(f"https://{handle}.breezy.hr/json")
            return isinstance(data, list) and len(data) > 0
        if vendor == "lever":
            data = await http.get_json(f"https://api.lever.co/v0/postings/{handle}?mode=json&limit=1")
            return isinstance(data, list) and len(data) > 0
        if vendor == "greenhouse":
            data = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs")
            return isinstance(data, dict) and bool(data.get("jobs"))
        if vendor == "ashby":
            data = await http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{handle}")
            return isinstance(data, dict) and bool(data.get("jobs"))
    except Exception:  # noqa: BLE001
        return False
    return False


async def detect_one(http, raw: str, log=None) -> Tuple[Optional[Dict[str, str]], str]:
    url = _normalize(raw)
    hit = find_in_text(url)
    if hit:
        return hit, "parsed from URL"

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [url] if parsed.path not in ("", "/") else []
    candidates += [base, base + "/careers", base + "/jobs"]
    seen = set()
    for page in candidates:
        if page in seen:
            continue
        seen.add(page)
        try:
            html_text = await http.get_text(page)
        except Exception:  # noqa: BLE001
            continue
        hit = find_in_text(html_text or "")
        if hit:
            return hit, f"found on {page}"

    stem = _stem(url)
    if stem:
        for vendor in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "breezy"):
            if await _probe(http, vendor, stem):
                return {"vendor": vendor, "handle": stem}, f"probed {vendor} API with handle '{stem}'"
    return None, "no supported ATS found on the site, its /careers or /jobs page, or by handle probe"
