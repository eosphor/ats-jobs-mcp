"""Vendor adapters: one function per ATS, all returning the same job shape.

Every adapter returns ``FetchResult``:
    jobs       list of job dicts (content fields; company optional)
    expected   int|None  — how many the source says exist (None if it does not say)
    capped     bool      — we stopped before exhausting the source
    source_url str       — the exact endpoint fetched (provenance per row)
    status     int       — upstream HTTP status

Only public, keyless endpoints are used. No login, no private API.
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from .errors import SilentFailureError
from .salary import parse_salary

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")
_DETAIL_CONCURRENCY = 6


def _strip_html(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    text = text.strip()
    return text or None


def _ms_to_iso(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _workday_posted_at(raw: Optional[str]) -> Optional[str]:
    """Normalize Workday's relative ``postedOn`` label to an ISO 8601 date.

    Workday returns labels like "Posted Today", "Posted Yesterday" or
    "Posted 5 Days Ago" instead of an absolute date, so we convert to an
    absolute UTC date at scrape time. "Posted 30+ Days Ago" is an unbounded
    lower bound (>= 30 days), so it is returned as None rather than a
    misleading exact date.
    """
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r'^Posted\s+(.+)$', raw, re.I)
    if m:
        label = m.group(1).strip().lower()
        today = datetime.now(timezone.utc).date()
        if label == 'today':
            return today.isoformat()
        if label == 'yesterday':
            return (today - timedelta(days=1)).isoformat()
        dm = re.match(r'^(\d+)\s+days?\s+ago$', label)
        if dm:
            days = int(dm.group(1))
            if days >= 30:
                return None
            return (today - timedelta(days=days)).isoformat()
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _lever_description(p: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for key in ("description", "additional"):
        v = p.get(key)
        if v:
            parts.append(_strip_html(v) or "")
    for lst in p.get("lists") or []:
        if not isinstance(lst, dict):
            continue
        if lst.get("text"):
            parts.append(str(lst["text"]))
        if lst.get("content"):
            parts.append(_strip_html(lst["content"]) or "")
    text = "\n".join(x for x in parts if x).strip()
    return text or None


def _job(**kw: Any) -> Dict[str, Any]:
    base = {
        "title": None,
        "company": None,
        "url": None,
        "location": None,
        "employmentType": None,
        "postedAt": None,
        "description": None,
        "department": None,
        "jobId": None,
        "remote": None,
        "salaryRaw": None,
        "salaryMin": None,
        "salaryMax": None,
        "salaryCurrency": None,
        "salaryPeriod": None,
    }
    base.update({k: v for k, v in kw.items() if k in base})
    return base


class FetchResult:
    def __init__(
        self,
        jobs: List[Dict[str, Any]],
        expected: Optional[int],
        capped: bool,
        source_url: str,
        status: int = 200,
    ) -> None:
        self.jobs = jobs
        self.expected = expected
        self.capped = capped
        self.source_url = source_url
        self.status = status


# ---------------------------------------------------------------------------
# Greenhouse — public boards API, meta.total gives the true count.
# ---------------------------------------------------------------------------
async def greenhouse(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"
    data = await http.get_json(url)
    raw = data.get("jobs") or []
    expected = (data.get("meta") or {}).get("total")
    expected = int(expected) if isinstance(expected, int) else None
    capped = len(raw) > limit
    jobs: List[Dict[str, Any]] = []
    for j in raw[:limit]:
        depts = ", ".join(d.get("name", "") for d in (j.get("departments") or []) if d.get("name"))
        loc = (j.get("location") or {}).get("name")
        jobs.append(_job(
            title=j.get("title"),
            company=j.get("company_name"),
            url=j.get("absolute_url"),
            location=loc,
            department=depts or None,
            description=_strip_html(j.get("content")),
            jobId=str(j.get("id")) if j.get("id") is not None else None,
            postedAt=j.get("first_published") or j.get("updated_at"),
        ))
    return FetchResult(jobs, expected, capped, url)


# ---------------------------------------------------------------------------
# Lever — public postings API (returns a bare JSON array; no total).
# ---------------------------------------------------------------------------
async def lever(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://api.lever.co/v0/postings/{handle}?mode=json"
    data = await http.get_json(url)
    if not isinstance(data, list):
        raise SilentFailureError(f"lever: expected array, got {type(data).__name__}", url=url)
    capped = len(data) > limit
    jobs: List[Dict[str, Any]] = []
    for p in data[:limit]:
        cats = p.get("categories") or {}
        jobs.append(_job(
            title=p.get("text"),
            company=handle,
            url=p.get("hostedUrl"),
            location=cats.get("location"),
            employmentType=cats.get("commitment"),
            department=cats.get("department") or cats.get("team"),
            description=_lever_description(p),
            jobId=str(p.get("id")) if p.get("id") is not None else None,
            postedAt=_ms_to_iso(p.get("createdAt")),
        ))
    return FetchResult(jobs, None, capped, url)


# ---------------------------------------------------------------------------
# Ashby — public posting API. Only isListed jobs are public.
# ---------------------------------------------------------------------------
async def ashby(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{handle}"
    data = await http.get_json(url)
    raw = [j for j in (data.get("jobs") or []) if j.get("isListed", True)]
    capped = len(raw) > limit
    jobs: List[Dict[str, Any]] = []
    for j in raw[:limit]:
        jobs.append(_job(
            title=j.get("title"),
            company=handle,
            url=j.get("jobUrl") or j.get("applyUrl"),
            location=j.get("location"),
            employmentType=j.get("employmentType"),
            department=j.get("department") or j.get("team"),
            description=_strip_html(j.get("descriptionHtml") or j.get("descriptionPlain")),
            jobId=str(j.get("id")) if j.get("id") is not None else None,
            postedAt=j.get("publishedAt"),
            remote=j.get("isRemote"),
        ))
    return FetchResult(jobs, None, capped, url)


# ---------------------------------------------------------------------------
# Recruitee — public offers API (no total).
# ---------------------------------------------------------------------------
async def recruitee(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://{handle}.recruitee.com/api/offers/"
    data = await http.get_json(url)
    raw = data.get("offers") or []
    capped = len(raw) > limit
    jobs: List[Dict[str, Any]] = []
    for o in raw[:limit]:
        jobs.append(_job(
            title=o.get("title"),
            company=o.get("company_name") or handle,
            url=o.get("careers_url") or o.get("careers_apply_url"),
            location=o.get("location") or ", ".join(
                x for x in (o.get("city"), o.get("country_code")) if x
            ) or None,
            employmentType=o.get("employment_type_code"),
            department=o.get("department"),
            description=_strip_html(o.get("description")),
            jobId=str(o.get("id")) if o.get("id") is not None else None,
            postedAt=o.get("published_at") or o.get("created_at"),
            remote=o.get("remote"),
        ))
    return FetchResult(jobs, None, capped, url)


# ---------------------------------------------------------------------------
# Teamtailor — public JSON Feed at /jobs.json, paged via next_url.
# ---------------------------------------------------------------------------
def _tt_location(job_location: Any) -> Optional[str]:
    if not isinstance(job_location, list) or not job_location:
        return None
    parts: List[str] = []
    for loc in job_location:
        addr = (loc or {}).get("address") or {}
        city = addr.get("addressLocality")
        country = addr.get("addressCountry")
        piece = ", ".join(x for x in (city, country) if x)
        if piece:
            parts.append(piece)
    return "; ".join(parts) or None


async def teamtailor(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url: Optional[str] = f"https://{handle}.teamtailor.com/jobs.json"
    first_url = url
    jobs: List[Dict[str, Any]] = []
    capped = False
    status = 200
    while url:
        data = await http.get_json(url)
        for it in data.get("items") or []:
            if len(jobs) >= limit:
                capped = True
                break
            jp = it.get("_jobposting") or {}
            ident = jp.get("identifier")
            ident_val = ident.get("value") if isinstance(ident, dict) else ident
            org = jp.get("hiringOrganization") or {}
            jobs.append(_job(
                title=it.get("title") or jp.get("title"),
                company=org.get("name") or handle,
                url=it.get("url"),
                location=_tt_location(jp.get("jobLocation")),
                employmentType=jp.get("employmentType"),
                department=None,
                description=_strip_html(it.get("content_html") or jp.get("description")),
                jobId=str(ident_val) if ident_val is not None else str(it.get("id")),
                postedAt=it.get("date_published") or jp.get("datePosted"),
            ))
        if capped:
            break
        url = data.get("next_url") or None
    return FetchResult(jobs, None, capped, first_url, status)


# ---------------------------------------------------------------------------
# Workday — public CXS API. Paginated (20/page); total is reported.
# ---------------------------------------------------------------------------
_WORKDAY_DESCRIPTION_CONCURRENCY = 3


async def workday(http, cfg, limit, include_descriptions=True) -> FetchResult:
    host = cfg["host"]
    tenant = cfg.get("tenant") or cfg["handle"] or host.split(".")[0]
    site = cfg["site"]
    endpoint = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    first_url = endpoint
    page_size = 20
    offset = 0
    total: Optional[int] = None
    raw: List[Dict[str, Any]] = []
    while len(raw) < limit:
        body = {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": ""}
        data = await http.post_json(endpoint, json_body=body)
        if total is None:
            total = data.get("total")
            total = int(total) if isinstance(total, int) else None
        postings = data.get("jobPostings") or []
        if not postings:
            break
        raw.extend(postings)
        offset += page_size
        if total is not None and offset >= total:
            break
    capped = (total is not None and total > limit) or (len(raw) > limit)
    selected = raw[:limit]
    descriptions: List[Optional[str]] = [None] * len(selected)
    if include_descriptions and selected:
        sem = asyncio.Semaphore(_WORKDAY_DESCRIPTION_CONCURRENCY)

        async def _bounded_description(posting: Dict[str, Any]) -> Optional[str]:
            ext = posting.get("externalPath") or ""
            if not ext:
                return None
            async with sem:
                return await _workday_description(http, host, tenant, site, ext)

        descriptions = list(
            await asyncio.gather(*(_bounded_description(p) for p in selected))
        )

    jobs: List[Dict[str, Any]] = []
    for p, description in zip(selected, descriptions):
        ext = p.get("externalPath") or ""
        url = f"https://{host}/en-US/{site}{ext}" if ext else None
        bullets = p.get("bulletFields") or []
        jobs.append(_job(
            title=p.get("title"),
            company=None,
            url=url,
            location=p.get("locationsText"),
            department=None,
            description=description,
            jobId=str(bullets[0]) if bullets else None,
            postedAt=_workday_posted_at(p.get("postedOn")),
        ))
    return FetchResult(jobs, total, capped, first_url)


async def _workday_description(http, host, tenant, site, ext) -> Optional[str]:
    try:
        data = await http.get_json(f"https://{host}/wday/cxs/{tenant}/{site}{ext}")
    except Exception:
        return None
    info = data.get("jobPostingInfo") or {}
    return _strip_html(info.get("jobDescription"))


# ---------------------------------------------------------------------------
# Personio — public XML feed (positions carry their own descriptions).
# ---------------------------------------------------------------------------
async def personio(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://{handle}.jobs.personio.de/xml"
    text = await http.get_text(url)
    if not text or "<workzag" not in text:
        raise SilentFailureError("personio: response is not a workzag XML feed", url=url)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SilentFailureError(f"personio: XML parse error: {exc}", url=url) from exc
    positions = root.findall("position")
    expected = len(positions)
    capped = expected > limit
    jobs: List[Dict[str, Any]] = []
    for pos in positions[:limit]:
        pid = pos.findtext("id")
        office = pos.findtext("office")
        extra_offices = [o.text for o in pos.findall("additionalOffices/office") if o.text]
        locations = ", ".join([x for x in [office] + extra_offices if x]) or None
        desc_parts = [
            node.text for node in pos.findall("jobDescriptions/jobDescription/value") if node.text
        ]
        jobs.append(_job(
            title=pos.findtext("name"),
            company=pos.findtext("subcompany") or handle,
            url=f"https://{handle}.jobs.personio.de/job/{pid}" if pid else None,
            location=locations,
            department=pos.findtext("department"),
            employmentType=pos.findtext("employmentType"),
            description=_strip_html(" ".join(desc_parts)) if desc_parts else None,
            jobId=pid,
            postedAt=pos.findtext("createdAt"),
        ))
    return FetchResult(jobs, expected, capped, url)


# ---------------------------------------------------------------------------
# Workable — public widget API (all jobs + descriptions in one GET).
# ---------------------------------------------------------------------------
async def workable(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://apply.workable.com/api/v1/widget/accounts/{handle}?details=true"
    data = await http.get_json(url)
    raw = data.get("jobs") or []
    expected = len(raw)
    capped = len(raw) > limit
    jobs: List[Dict[str, Any]] = []
    for j in raw[:limit]:
        location = ", ".join(x for x in (j.get("city"), j.get("country")) if x) or None
        shortcode = j.get("shortcode")
        jobs.append(_job(
            title=j.get("title"),
            company=data.get("name") or None,
            url=j.get("url") or j.get("shortlink"),
            location=location,
            employmentType=j.get("employment_type"),
            department=j.get("department"),
            description=_strip_html(j.get("description")) if include_descriptions else None,
            jobId=shortcode,
            postedAt=j.get("published_on"),
            remote=j.get("telecommuting"),
        ))
    return FetchResult(jobs, expected, capped, url)


# ---------------------------------------------------------------------------
# SmartRecruiters — public postings API. The listing carries title/location/
# department/type; description and salary live in the (extra-call) detail
# endpoint and are left null here.
# ---------------------------------------------------------------------------
async def _smartrecruiters_description(http, handle: str, pid: str) -> Optional[str]:
    try:
        data = await http.get_json(
            f"https://api.smartrecruiters.com/v1/companies/{handle}/postings/{pid}"
        )
    except Exception:  # noqa: BLE001
        return None
    sections = (data.get("jobAd") or {}).get("sections") or {}
    jd = sections.get("jobDescription")
    if isinstance(jd, dict):
        jd = jd.get("text")
    return _strip_html(jd)


async def smartrecruiters(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://api.smartrecruiters.com/v1/companies/{handle}/postings"
    page_size = min(limit, 100)
    offset = 0
    total: Optional[int] = None
    raw: List[Dict[str, Any]] = []
    while len(raw) < limit:
        data = await http.get_json(url, params={"limit": page_size, "offset": offset})
        if total is None:
            total = data.get("totalFound")
            total = int(total) if isinstance(total, int) else None
        items = data.get("content") or []
        if not items:
            break
        raw.extend(items)
        offset += len(items)
        if total is not None and offset >= total:
            break
    capped = (total is not None and total > limit) or (len(raw) > limit)
    selected = raw[:limit]
    descriptions: List[Optional[str]] = [None] * len(selected)
    if include_descriptions and selected:
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def _bounded(posting: Dict[str, Any]) -> Optional[str]:
            pid = str(posting.get("id") or posting.get("uuid") or "")
            if not pid:
                return None
            async with sem:
                return await _smartrecruiters_description(http, handle, pid)

        descriptions = list(await asyncio.gather(*(_bounded(p) for p in selected)))

    jobs: List[Dict[str, Any]] = []
    for p, description in zip(selected, descriptions):
        loc = p.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
        ) or None
        dept = p.get("department") or {}
        dept_name = dept.get("label") if isinstance(dept, dict) else None
        emp = p.get("typeOfEmployment") or {}
        emp_name = emp.get("label") if isinstance(emp, dict) else None
        pid = p.get("id") or p.get("uuid")
        jobs.append(_job(
            title=p.get("name"),
            company=None,
            url=f"https://jobs.smartrecruiters.com/{handle}/{pid}" if pid else None,
            location=location,
            employmentType=emp_name,
            department=dept_name,
            description=description,
            jobId=str(pid) if pid else None,
            postedAt=p.get("releasedDate"),
            remote=bool(loc.get("remote")),
        ))
    return FetchResult(jobs, total, capped, url)


# ---------------------------------------------------------------------------
# Breezy HR — public JSON board at /json (top-level array, no pagination).
# ---------------------------------------------------------------------------
def _breezy_location(loc: Any) -> Optional[str]:
    if not isinstance(loc, dict):
        return None
    parts: List[str] = []
    if loc.get("city"):
        parts.append(str(loc["city"]))
    country = loc.get("country") or {}
    if isinstance(country, dict) and country.get("name"):
        parts.append(str(country["name"]))
    return ", ".join(parts) or loc.get("name") or None


async def _breezy_description(http, job_url: str) -> Optional[str]:
    try:
        text = await http.get_text(job_url)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', text, re.I)
    if not m:
        return None
    desc = html.unescape(m.group(1))
    desc = _WS_RE.sub(" ", desc).strip()
    return desc or None


async def breezy(http, cfg, limit, include_descriptions=True) -> FetchResult:
    handle = cfg["handle"]
    url = f"https://{handle}.breezy.hr/json"
    data = await http.get_json(url)
    if not isinstance(data, list):
        raise SilentFailureError(f"breezy: expected array, got {type(data).__name__}", url=url)
    capped = len(data) > limit
    selected = data[:limit]
    descriptions: List[Optional[str]] = [None] * len(selected)
    if include_descriptions and selected:
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def _bounded(p: Dict[str, Any]) -> Optional[str]:
            job_url = p.get("url")
            if not job_url:
                return None
            async with sem:
                return await _breezy_description(http, job_url)

        descriptions = list(await asyncio.gather(*(_bounded(p) for p in selected)))

    jobs: List[Dict[str, Any]] = []
    for p, description in zip(selected, descriptions):
        loc = p.get("location")
        emp = p.get("type") or {}
        emp_name = emp.get("name") if isinstance(emp, dict) else None
        salary_raw = p.get("salary") or None
        salary_min, salary_max, salary_cur, salary_period = parse_salary(salary_raw)
        jobs.append(_job(
            title=p.get("name"),
            company=None,
            url=p.get("url"),
            location=_breezy_location(loc),
            employmentType=emp_name,
            department=p.get("department"),
            description=description,
            jobId=str(p.get("id")) if p.get("id") else None,
            postedAt=p.get("published_date"),
            remote=bool(loc.get("is_remote")) if isinstance(loc, dict) else None,
            salaryRaw=salary_raw,
            salaryMin=salary_min,
            salaryMax=salary_max,
            salaryCurrency=salary_cur,
            salaryPeriod=salary_period,
        ))
    return FetchResult(jobs, None, capped, url)


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
    "personio": personio,
    "recruitee": recruitee,
    "teamtailor": teamtailor,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "breezy": breezy,
}
