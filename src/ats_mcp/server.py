"""ATS Jobs MCP server.

Search live job postings from public applicant-tracking-system (ATS) APIs:
Greenhouse, Lever, Ashby, Workday, Personio, Recruitee, Teamtailor, Workable,
SmartRecruiters and Breezy HR. No API keys, no login, no Apify calls.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import service
from .vendors import ADAPTERS

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP(
    name="ATS Jobs",
    json_response=True,
    stateless_http=True,
    instructions=(
        "Live job postings from 10 applicant tracking systems (Greenhouse, Lever, Ashby, "
        "Workday, Personio, Recruitee, Teamtailor, Workable, SmartRecruiters, Breezy HR). "
        "Call search_jobs with a company website or careers URL; detection is automatic. "
        "Use keywords, location and remote to narrow results. For only-new monitoring, pass "
        "the previous response's latest_posted_at as posted_after. "
        "Jobs are read from public ATS APIs at request time and cached for 5 minutes."
    ),
)


@mcp.tool(annotations=READ_ONLY)
async def search_jobs(
    company: Optional[str] = None,
    vendor: Optional[str] = None,
    handle: Optional[str] = None,
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    remote: bool = False,
    posted_after: Optional[str] = None,
    limit: int = 20,
    include_descriptions: bool = False,
) -> Dict[str, Any]:
    """Search live job postings for one company.

    Provide either `company` (website, domain or careers URL - the ATS is detected
    automatically) or `vendor` + `handle` (e.g. vendor="greenhouse", handle="stripe").
    Workday companies must use a careers URL, e.g.
    https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite.

    Filters:
    - keywords: space-separated terms, all must appear in title, department,
      location, company or description (AND match, case-insensitive).
    - location: substring match on the job location, e.g. "Berlin" or "Paris".
    - remote: true keeps only remote-friendly roles.
    - posted_after: ISO date or datetime; returns only jobs posted on/after it.
      For only-new monitoring pass the previous response's `latest_posted_at`.

    limit: 1-50 (default 20). include_descriptions adds a truncated description
    (slower on Workday).

    Returns job rows with title, company, location, remote, department,
    employment_type, posted_at, url, ats, job_id, optional salary, plus
    `latest_posted_at` to use as the next cursor.
    """
    try:
        return await service.search(
            company=company,
            vendor=vendor,
            handle=handle,
            keywords=keywords,
            location=location,
            remote=remote,
            posted_after=posted_after,
            limit=limit,
            include_descriptions=include_descriptions,
        )
    except ValueError as exc:
        return {"ok": False, "error": "invalid_request", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)[:400]}


@mcp.tool(annotations=READ_ONLY)
async def search_jobs_multi(
    companies: List[str],
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    remote: bool = False,
    posted_after: Optional[str] = None,
    limit_per_company: int = 10,
    include_descriptions: bool = False,
) -> Dict[str, Any]:
    """Search several companies at once (up to 5), e.g. a target-account list.

    Each company can be a website, domain or careers URL. Returns per-company
    results plus a combined `jobs` list. `limit_per_company` is 1-20 (default 10).
    Filters behave exactly like in search_jobs.
    """
    try:
        return await service.search_many(
            companies=companies,
            keywords=keywords,
            location=location,
            remote=remote,
            posted_after=posted_after,
            limit=limit_per_company,
            include_descriptions=include_descriptions,
        )
    except ValueError as exc:
        return {"ok": False, "error": "invalid_request", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)[:400]}


@mcp.tool(annotations=READ_ONLY)
async def detect_company_ats(company: str) -> Dict[str, Any]:
    """Detect which ATS a company uses, without fetching its jobs.

    Takes a website, domain or careers URL and returns the ATS vendor, handle
    and how it was detected (URL pattern, careers page, or API probe). Useful to
    confirm a company is supported before calling search_jobs.
    """
    try:
        cfg, note = await service.resolve_source(company=company)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)[:300]}
    if not cfg:
        return {
            "ok": False,
            "company": company,
            "error": "ats_not_detected",
            "detection": note,
            "hint": "Try the exact careers URL, or pass vendor + handle to search_jobs.",
        }
    return {"ok": True, "company": company, "ats": cfg, "detection": note}


@mcp.tool(annotations=READ_ONLY)
def list_supported_ats() -> Dict[str, Any]:
    """List the 10 supported ATS vendors and what each needs as input."""
    details = {
        "greenhouse": "handle = board token, e.g. 'stripe' (boards.greenhouse.io/stripe)",
        "lever": "handle = company slug, e.g. 'netflix' (jobs.lever.co/netflix)",
        "ashby": "handle = job board name, e.g. 'notion' (jobs.ashbyhq.com/notion)",
        "workday": "careers URL required: https://<tenant>.wdN.myworkdayjobs.com/<site>",
        "personio": "handle = subdomain, e.g. 'personio' (<handle>.jobs.personio.de)",
        "recruitee": "handle = subdomain, e.g. 'acme' (<handle>.recruitee.com)",
        "teamtailor": "handle = subdomain, e.g. 'acme' (<handle>.teamtailor.com)",
        "workable": "handle = account, e.g. 'acme' (apply.workable.com/acme)",
        "smartrecruiters": "handle = company id, e.g. 'Visa' (careers.smartrecruiters.com/Visa)",
        "breezy": "handle = subdomain, e.g. 'acme' (<handle>.breezy.hr)",
    }
    return {
        "ok": True,
        "count": len(ADAPTERS),
        "vendors": [{"vendor": v, "input": details[v]} for v in sorted(ADAPTERS)],
        "tips": [
            "Pass any company website to search_jobs; the ATS is detected automatically.",
            "Companies without a supported public ATS return ats_not_detected (no charge, no data).",
            "Workday paginates 20 jobs per page; requests are concurrency-limited.",
        ],
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "ats-jobs", "tools": 4})


def main() -> None:
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        show_banner=False,
    )


if __name__ == "__main__":
    main()
