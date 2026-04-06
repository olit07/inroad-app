"""
Unit tests for score_lead_v2 (Scoring v2 — Epic 2).
"""
import unittest
from pipeline.matcher import score_lead_v2


# ── Minimal shared fixtures ───────────────────────────────────────────────────

def _make_lead(is_alumni=False, title="Senior Analyst", tenure_months=24):
    return {
        "name":          "Jane Smith",
        "title":         title,
        "company":       "Goldman Sachs",
        "university":    "UCL",
        "linkedin_url":  "https://linkedin.com/in/jane-smith",
        "tenure_months": tenure_months,
        "is_alumni":     is_alumni,
    }


def _make_job(title="Risk Analyst", industry="Finance", company_size="large"):
    return {
        "id":           1,
        "title":        title,
        "company_name": "Goldman Sachs",
        "industry":     industry,
        "company_size": company_size,
        "url":          "https://example.com/job/1",
    }


def _make_student(industries=None, company_size="large"):
    return {
        "id":          42,
        "first_name":  "Alex",
        "university":  "UCL",
        "status":      "intern",
        "industries":  '["Finance", "Investment Banking"]' if industries is None else industries,
        "company_size": company_size,
        "region":      "UK",
    }


# ── Test class ────────────────────────────────────────────────────────────────

class TestScoreLeadV2(unittest.TestCase):

    def test_breakdown_keys(self):
        """score_lead_v2 must return a breakdown with exactly the 6 expected keys."""
        lead    = _make_lead()
        job     = _make_job()
        student = _make_student()

        score, breakdown = score_lead_v2(lead, job, student)

        expected_keys = {
            "industry_match",
            "company_size",
            "title_relevance",
            "alumni",
            "seniority_fit",
            "tenure_fit",
        }
        self.assertEqual(set(breakdown.keys()), expected_keys,
                         f"Unexpected breakdown keys: {set(breakdown.keys())}")

    def test_industry_match_boosts_score(self):
        """A student whose industries include the job industry scores ≥25 pts higher."""
        lead = _make_lead()
        job  = _make_job(industry="Finance")

        student_match    = _make_student(industries='["Finance", "Technology"]')
        student_no_match = _make_student(industries='["Healthcare", "Media"]')

        score_match,    _ = score_lead_v2(lead, job, student_match)
        score_no_match, _ = score_lead_v2(lead, job, student_no_match)

        self.assertGreaterEqual(
            score_match - score_no_match, 25,
            f"Expected ≥25 pt gap, got {score_match} vs {score_no_match}"
        )

    def test_alumni_boosts_score(self):
        """A lead with is_alumni=True should score exactly 20 pts more than is_alumni=False."""
        job     = _make_job()
        student = _make_student()

        lead_alumni    = _make_lead(is_alumni=True)
        lead_non_alumni = _make_lead(is_alumni=False)

        score_alumni,     bd_alumni     = score_lead_v2(lead_alumni,     job, student)
        score_non_alumni, bd_non_alumni = score_lead_v2(lead_non_alumni, job, student)

        self.assertEqual(bd_alumni["alumni"],     20.0)
        self.assertEqual(bd_non_alumni["alumni"],  0.0)
        self.assertAlmostEqual(
            score_alumni - score_non_alumni, 20.0, places=1,
            msg=f"Expected 20 pt alumni gap, got {score_alumni} vs {score_non_alumni}"
        )


if __name__ == "__main__":
    unittest.main()
