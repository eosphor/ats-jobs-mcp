# ATS Jobs MCP server

Search live job postings straight from public applicant-tracking-system (ATS) APIs. Give a company website or an ATS handle and get normalized, filterable results — no API keys, no login, no scraping of job boards.

Built on the same data model as the [ATS Jobs API & Hiring Signals](https://apify.com/suitable_sunspot/keyless-ats-jobs-api) Apify Actor, but this server calls the ATS endpoints directly, so it runs standalone.

## Supported ATS

Greenhouse · Lever · Ashby · Workday · Personio · Recruitee · Teamtailor · Workable · SmartRecruiters · Breezy HR

Most competitors cover only Greenhouse. One tool call here reaches the long tail of European and enterprise ATS, including Workday tenants.

## Tools

| Tool | What it does |
| --- | --- |
| `search_jobs` | Jobs for one company. Pass a website/domain/careers URL (ATS auto-detected) or `vendor` + `handle`. Filters: keywords, location, remote, `posted_after`, limit 1–50 |
| `search_jobs_multi` | Same search for up to 5 companies at once, with per-company results and a combined list |
| `detect_company_ats` | Detect which ATS a company uses, without fetching jobs |
| `list_supported_ats` | The 10 vendors and the input each one needs |

Every response includes `latest_posted_at`. Pass it back as `posted_after` to get only jobs posted since the previous call — that is the "only new" mode, kept stateless so it works for any number of users.

## Example prompts

- "Find data engineering jobs at Spotify posted in the last two weeks."
- "Which ATS does nvidia.com use?"
- "Remote product design roles at Notion, Linear and Figma."
- "Watch these 5 target accounts for new sales roles; return only postings newer than 2026-10-01."

## Example calls and real output

`search_jobs` with `{"company": "stripe.com", "limit": 3}`:

```json
{
  "ok": true,
  "ats": {"vendor": "greenhouse", "handle": "stripe", "detected_from": "probed greenhouse API with handle 'stripe'"},
  "total_available": 732,
  "matched": 732,
  "returned": 3,
  "truncated": true,
  "latest_posted_at": "2026-10-16T09:31:53-04:00",
  "source_url": "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
  "jobs": [
    {
      "title": "Account Executive, Enterprise Grower (Benelux)",
      "company": "Stripe",
      "location": "Amsterdam",
      "remote": false,
      "department": "8501 Sales",
      "posted_at": "2026-10-16T09:31:53-04:00",
      "url": "https://stripe.com/jobs/search?gh_jid=...",
      "ats": "greenhouse",
      "job_id": "..."
    }
  ]
}
```

Workday (`{"company": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "limit": 5}`) returns 5 of 2000 postings in ~2.6 s; descriptions are opt-in because each one costs an extra request.

`search_jobs_multi` with three companies returns `companies_ok: 3`, per-company blocks and a combined `jobs` list.

`search_jobs` with an unknown company returns `{"ok": false, "error": "ats_not_detected"}` plus a hint — no empty success.

## Only-new monitoring

The server is stateless. To poll for new roles:

1. Call `search_jobs`; keep `latest_posted_at`.
2. Next run, pass it as `posted_after` (ISO date or datetime).
3. Jobs without a published date are excluded while the filter is active.

This works for scheduled agents without server-side per-user state.

## Limits and behaviour

- `limit` 1–50 (default 20); `search_jobs_multi` 1–20 per company, max 5 companies.
- Responses are cached for 5 minutes per company and ATS, so repeated calls are instant.
- Workday requests are concurrency-limited (2 at a time) and paginate 20 jobs per page.
- Descriptions are truncated to 1500 characters and returned only with `include_descriptions: true`.
- Data comes from public ATS endpoints that companies expose for their own careers pages. No login, no bypassing access controls.

## Run locally

```bash
pip install -r requirements.txt
PORT=8080 python src/server.py
# MCP endpoint: http://localhost:8080/mcp   health: /health
```

## Deploy notes

`mcpize.yaml` targets Python 3.11+, HTTP transport on `$PORT`, entry `src/server.py`.
`GET /health` returns `{"status":"ok"}`.

## Pricing on MCPize

- **Free** — 100 tool calls/month, all tools, 10 results per call
- **Pro $12/month** — 10,000 calls/month, full limits, priority support

## License

MIT
