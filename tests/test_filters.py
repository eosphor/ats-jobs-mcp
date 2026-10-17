import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest

from ats_mcp import service


def job(**kw):
    base = {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin",
        "department": "Engineering",
        "description": None,
        "remote": None,
        "postedAt": "2026-10-01T10:00:00Z",
    }
    base.update(kw)
    return base


class MatchesTest(unittest.TestCase):
    def test_keyword_all_tokens_required(self):
        j = job(title="Senior Data Engineer")
        self.assertTrue(service._matches(j, "data engineer", None, False, None))
        self.assertFalse(service._matches(j, "data designer", None, False, None))

    def test_keyword_case_insensitive(self):
        self.assertTrue(service._matches(job(title="SALES Manager"), "sales", None, False, None))

    def test_location_substring(self):
        self.assertTrue(service._matches(job(location="Paris, France"), None, "paris", False, None))
        self.assertFalse(service._matches(job(location="Lyon"), None, "paris", False, None))

    def test_remote_flag(self):
        self.assertTrue(service._matches(job(remote=True), None, None, True, None))
        self.assertTrue(service._matches(job(location="Remote - EU"), None, None, True, None))
        self.assertFalse(service._matches(job(location="Berlin"), None, None, True, None))

    def test_posted_after_excludes_old_and_undated(self):
        cutoff = service._parse_date("2026-10-01")
        self.assertTrue(service._matches(job(postedAt="2026-10-02T00:00:00Z"), None, None, False, cutoff))
        self.assertFalse(service._matches(job(postedAt="2026-09-30T23:59:59Z"), None, None, False, cutoff))
        self.assertFalse(service._matches(job(postedAt=None), None, None, False, cutoff))

    def test_parse_date_accepts_date_and_datetime(self):
        self.assertIsNotNone(service._parse_date("2026-10-01"))
        self.assertIsNotNone(service._parse_date("2026-10-01T12:30:00+02:00"))
        self.assertIsNone(service._parse_date("not-a-date"))


class ShapeTest(unittest.TestCase):
    def test_shape_omits_description_by_default(self):
        out = service._shape(job(description="x" * 5000), "greenhouse", False)
        self.assertNotIn("description", out)
        self.assertEqual(out["ats"], "greenhouse")
        self.assertFalse(out["remote"])

    def test_shape_truncates_description(self):
        out = service._shape(job(description="x" * 5000), "lever", True)
        self.assertEqual(len(out["description"]), 1503)
        self.assertTrue(out["description"].endswith("..."))

    def test_salary_only_when_present(self):
        self.assertNotIn("salary", service._shape(job(), "lever", False))
        out = service._shape(job(salaryRaw="$120k", salaryMin=120000), "lever", False)
        self.assertEqual(out["salary"]["raw"], "$120k")


if __name__ == "__main__":
    unittest.main()
