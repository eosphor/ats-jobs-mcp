"""Search service: ATS detection, cached fetches, filtering and shaping.

Standalone: talks to public ATS endpoints directly, no Apify, no API keys.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .detect import detect_one
from .http_client import HttpClient
from .vendors import ADAPTERS, FetchResult

CACHE_TTL_SECONDS = 300
DETECT_TTL_SECONDS = 3600
MAX_FETCH = 100
MAX_LIMIT = 50
MAX_COMPANIES = 5

_cache: Dict[Tuple[str, str, bool, int], Tuple[float, FetchResult]] = {}
_detect_cache: Dict[str, Tuple[float, Optional[Dict[str, str]], str]] = {}
_workday_sem = asyncio.Semaphore(2)
_cache_lock = asyncio.Lock()


def _epoch() -> float:
    return time.monotonic()


def _bucket(limit: int) -> int:
    if limit <= 10:
        return 25
    if limit <= 20:
        return 50
    return MAX_FETCH


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _is_remote(job: Dict[str, Any]) -> bool:
    if job.get("remote") is True:
        return True
    location = (job.get("location") or "").lower()
    return any(token in location for token in ("remote", "télétravail", "teletravail", "home office", "anywhere"))


def _shape(job: Dict[str, Any], vendor: str, include_descriptions: bool) -> Dict[str, Any]:
    description = job.get("description")
    if description and len(description) > 1500:
        description = description[:1500].rstrip() + "..."
    out: Dict[str, Any] = {
        "title": _clean(job.get("title")),
        "company": _clean(job.get("company")),
        "location": _clean(job.get("location")),
        "remote": _is_remote(job),
        "department": _clean(job.get("department")),
        "employment_type": _clean(job.get("employmentType")),
        "posted_at": _clean(job.get("postedAt")),
        "url": _clean(job.get("url")),
        "ats": vendor,
        "job_id": _clean(job.get("jobId")),
    }
    salary = {
        "raw": _clean(job.get("salaryRaw")),
        "min": job.get("salaryMin"),
        "max": job.get("salaryMax"),
        "currency": _clean(job.get("salaryCurrency")),
        "period": _clean(job.get("salaryPeriod")),
    }
    if any(v is not None for v in salary.values()):
        out["salary"] = salary
    if include_descriptions:
        out["description"] = description
    return out


def _matches(
    job: Dict[str, Any],
    keywords: Optional[str],
    location: Optional[str],
    remote: bool,
    posted_after: Optional[datetime],
) -> bool:
    if location:
        needle = location.casefold()
        if needle not in ((job.get("location") or "").casefold()):
            return False
    if remote and not _is_remote(job):
        return False
    if posted_after is not None:
        posted = _parse_date(job.get("postedAt"))
        if posted is None or posted < posted_after:
            return False
    if keywords:
        tokens = [t for t in keywords.casefold().replace(",", " ").split() if t]
        haystack = " ".join(
            str(job.get(field) or "")
            for field in ("title", "company", "location", "department", "description")
        ).casefold()
        if not all(token in haystack for token in tokens):
            return False
    return True


async def resolve_source(
    company: Optional[str] = None,
    vendor: Optional[str] = None,
    handle: Optional[str] = None,
) -> Tuple[Optional[Dict[str, str]], str]:
    """Return (source config, detection note). Config has vendor + handle (+ workday fields)."""
    if vendor:
        vendor = vendor.strip().lower()
        if vendor not in ADAPTERS:
            raise ValueError(
                f"Unknown ATS '{vendor}'. Supported: {', '.join(sorted(ADAPTERS))}."
            )
        if not handle:
            raise ValueError("Pass 'handle' together with 'vendor' (e.g. vendor='greenhouse', handle='stripe').")
        if vendor == "workday":
            raise ValueError(
                "Workday needs host and site: pass the careers URL as 'company' instead "
                "(e.g. https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite)."
            )
        return {"vendor": vendor, "handle": handle.strip()}, "explicit vendor + handle"

    if not company:
        raise ValueError("Pass 'company' (website or careers URL) or 'vendor' + 'handle'.")

    key = company.strip().lower()
    async with _cache_lock:
        hit = _detect_cache.get(key)
    if hit and _epoch() - hit[0] < DETECT_TTL_SECONDS:
        return hit[1], hit[2] + " (cached)"

    async with HttpClient(timeout=15.0, max_retries=2) as http:
        cfg, note = await detect_one(http, company)

    async with _cache_lock:
        _detect_cache[key] = (_epoch(), cfg, note)
    return cfg, note


async def fetch(
    cfg: Dict[str, str],
    limit: int,
    include_descriptions: bool,
) -> Tuple[FetchResult, bool]:
    """Fetch up to a bounded bucket of jobs, with a short TTL cache.

    Returns (result, cached).
    """
    vendor = cfg["vendor"]
    handle = cfg.get("handle") or cfg.get("site") or ""
    cap = _bucket(limit)
    key = (vendor, handle.lower(), include_descriptions, cap)

    async with _cache_lock:
        hit = _cache.get(key)
    if hit and _epoch() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1], True

    adapter = ADAPTERS[vendor]
    async with HttpClient(timeout=20.0, max_retries=2) as http:
        if vendor == "workday":
            async with _workday_sem:
                result = await adapter(http, cfg, cap, include_descriptions)
        else:
            result = await adapter(http, cfg, cap, include_descriptions)

    async with _cache_lock:
        if len(_cache) > 128:
            _cache.clear()
        _cache[key] = (_epoch(), result)
    return result, False


async def search(
    company: Optional[str],
    vendor: Optional[str],
    handle: Optional[str],
    keywords: Optional[str],
    location: Optional[str],
    remote: bool,
    posted_after: Optional[str],
    limit: int,
    include_descriptions: bool,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    cutoff = _parse_date(posted_after)
    if posted_after and cutoff is None:
        raise ValueError(f"posted_after is not a valid ISO date/datetime: {posted_after!r}")

    started = time.monotonic()
    cfg, note = await resolve_source(company=company, vendor=vendor, handle=handle)
    if not cfg:
        return {
            "ok": False,
            "error": "ats_not_detected",
            "hint": (
                "No supported ATS found. Pass an explicit careers URL "
                "(boards.greenhouse.io/acme, jobs.lever.co/acme, jobs.ashbyhq.com/acme, "
                "<tenant>.wd5.myworkdayjobs.com/<site>) or vendor + handle."
            ),
            "detection": note,
        }

    result, cached = await fetch(cfg, limit, include_descriptions)
    matched = [
        j
        for j in result.jobs
        if _matches(j, keywords, location, remote, cutoff)
    ]
    matched.sort(key=lambda j: j.get("postedAt") or "", reverse=True)
    picked = matched[:limit]
    jobs = [_shape(j, cfg["vendor"], include_descriptions) for j in picked]
    latest = next((j["posted_at"] for j in jobs if j.get("posted_at")), None)

    total_available = result.expected

    return {
        "ok": True,
        "ats": {"vendor": cfg["vendor"], "handle": cfg.get("handle") or cfg.get("site"), "detected_from": note},
        "query": {
            "company": company,
            "keywords": keywords,
            "location": location,
            "remote_only": bool(remote),
            "posted_after": posted_after,
            "limit": limit,
        },
        "total_available": total_available,
        "matched": len(matched),
        "returned": len(jobs),
        "truncated": len(matched) > len(jobs) or bool(result.capped),
        "latest_posted_at": latest,
        "source_url": result.source_url,
        "cached": cached,
        "took_ms": int((time.monotonic() - started) * 1000),
        "jobs": jobs,
    }


async def search_many(
    companies: List[str],
    keywords: Optional[str],
    location: Optional[str],
    remote: bool,
    posted_after: Optional[str],
    limit: int,
    include_descriptions: bool,
) -> Dict[str, Any]:
    companies = [c for c in (companies or []) if c and c.strip()]
    if not companies:
        raise ValueError("Pass at least one company.")
    companies = companies[:MAX_COMPANIES]
    limit = max(1, min(int(limit or 10), 20))
    sem = asyncio.Semaphore(3)

    async def one(name: str) -> Dict[str, Any]:
        async with sem:
            try:
                res = await search(
                    name, None, None, keywords, location, remote, posted_after, limit, include_descriptions
                )
                res["company_input"] = name
                return res
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "company_input": name, "error": type(exc).__name__, "message": str(exc)[:300]}

    results = await asyncio.gather(*(one(c) for c in companies))
    jobs = [j for r in results if r.get("ok") for j in r.get("jobs", [])]
    return {
        "ok": True,
        "companies_requested": len(companies),
        "companies_ok": sum(1 for r in results if r.get("ok")),
        "returned": len(jobs),
        "latest_posted_at": max((j.get("posted_at") or "" for j in jobs), default=None) or None,
        "results": results,
        "jobs": jobs,
    }
