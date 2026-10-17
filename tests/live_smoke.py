"""Live smoke test: real ATS calls, no mocks.

Run: PYTHONPATH=src python tests/live_smoke.py
Prints one line per case and exits non-zero if any tool returns nothing useful.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ats_mcp import service  # noqa: E402

CASES = [
    ("greenhouse", dict(company="stripe.com", limit=3)),
    ("lever", dict(company="https://jobs.lever.co/spotify", keywords="engineer", limit=3)),
    ("ashby", dict(company="notion.so", remote=True, limit=3)),
    ("workday", dict(company="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", limit=3)),
    ("personio", dict(company="https://personio.jobs.personio.de", limit=2)),
    ("teamtailor", dict(company="https://storytel.teamtailor.com", limit=2)),
]


async def main() -> int:
    failures = 0
    for name, kwargs in CASES:
        try:
            res = await service.search(
                company=kwargs.get("company"), vendor=None, handle=None,
                keywords=kwargs.get("keywords"), location=None,
                remote=kwargs.get("remote", False), posted_after=None,
                limit=kwargs["limit"], include_descriptions=False,
            )
            ok = bool(res.get("ok") and res.get("returned"))
            print(f"{name:11s} ok={ok} returned={res.get('returned')} total={res.get('total_available')} "
                  f"ms={res.get('took_ms')} first={(res.get('jobs') or [{}])[0].get('title')!r}")
            if ok:
                latest = res.get("latest_posted_at")
                follow = await service.search(
                    company=kwargs.get("company"), vendor=None, handle=None, keywords=None,
                    location=None, remote=False, posted_after=latest,
                    limit=2, include_descriptions=False,
                )
                print(f"{'':11s} cursor={latest} newer_after_cursor={follow.get('returned')}")
            failures += 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            print(f"{name:11s} ERROR {type(exc).__name__}: {exc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
