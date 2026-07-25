"""
Contact intelligence tests — gate extraction.

Ihar Mahniok fixture: gate research found his personal email, LinkedIn and X.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from contra.intelligence.contact_extract import (
    _extract_from_text,
    _extract_from_analyst_facts,
    extract_and_persist_gate_contacts,
)

# ---------------------------------------------------------------------------
# Ihar Mahniok fixture data
# ---------------------------------------------------------------------------

IHAR_WEB_CONTEXT = """
Ihar Mahaniok is an individual LP and angel investor based in Eastern Europe.
His personal website mentions he can be reached at i@mahaniok.com for
investment opportunities.
LinkedIn: https://www.linkedin.com/in/imahaniok
X/Twitter profile: https://x.com/imahaniok
He has backed several early-stage funds focused on AI and emerging markets.
"""

IHAR_ANALYST_FACTS = [
    "email: i@mahaniok.com",
    "linkedin: https://linkedin.com/in/imahaniok",
]


# ---------------------------------------------------------------------------
# Gate text extraction
# ---------------------------------------------------------------------------

class TestExtractFromText(unittest.TestCase):

    def test_extracts_personal_email(self):
        emails, _, _ = _extract_from_text(IHAR_WEB_CONTEXT)
        self.assertIn("i@mahaniok.com", emails)

    def test_extracts_linkedin_url(self):
        _, linkedin, _ = _extract_from_text(IHAR_WEB_CONTEXT)
        self.assertTrue(any("imahaniok" in u for u in linkedin))

    def test_extracts_twitter_url(self):
        _, _, twitter = _extract_from_text(IHAR_WEB_CONTEXT)
        self.assertTrue(any("imahaniok" in u for u in twitter))

    def test_filters_noreply(self):
        text = "Contact noreply@example.com for support"
        emails, _, _ = _extract_from_text(text)
        self.assertEqual(emails, [])

    def test_filters_known_noise_domain(self):
        text = "Image hosted at cdn@cloudinary.com"
        emails, _, _ = _extract_from_text(text)
        self.assertEqual(emails, [])

    def test_filters_linkedin_company_page(self):
        text = "Company page: https://linkedin.com/company/acme-corp"
        _, linkedin, _ = _extract_from_text(text)
        self.assertEqual(linkedin, [])


class TestExtractFromAnalystFacts(unittest.TestCase):

    def test_extracts_analyst_email(self):
        emails, _, _ = _extract_from_analyst_facts(IHAR_ANALYST_FACTS)
        self.assertIn("i@mahaniok.com", emails)

    def test_extracts_analyst_linkedin(self):
        _, linkedin, _ = _extract_from_analyst_facts(IHAR_ANALYST_FACTS)
        self.assertTrue(any("imahaniok" in u for u in linkedin))


# ---------------------------------------------------------------------------
# extract_and_persist_gate_contacts (mocked DB)
# ---------------------------------------------------------------------------

class TestExtractAndPersist(unittest.TestCase):

    def _mock_con(self):
        con = MagicMock()
        con.execute.return_value.fetchone.return_value = None
        return con

    def test_returns_correct_counts_for_ihar(self):
        con = self._mock_con()
        stats = extract_and_persist_gate_contacts(
            con,
            lp_name="Ihar Mahniok",
            allocator_id="alloc-001",
            web_context=IHAR_WEB_CONTEXT,
        )
        self.assertEqual(stats["gate_emails"], 1)
        self.assertGreaterEqual(stats["gate_linkedin"], 1)
        self.assertGreaterEqual(stats["gate_twitter"], 1)

    def test_analyst_facts_counted_separately(self):
        con = self._mock_con()
        stats = extract_and_persist_gate_contacts(
            con,
            lp_name="Ihar Mahniok",
            allocator_id="alloc-001",
            web_context="",
            analyst_facts=IHAR_ANALYST_FACTS,
        )
        self.assertGreater(stats["analyst_overrides"], 0)

    def test_no_crash_on_empty_context(self):
        con = self._mock_con()
        stats = extract_and_persist_gate_contacts(
            con,
            lp_name="Nobody",
            allocator_id="alloc-999",
            web_context="",
        )
        self.assertEqual(stats["gate_emails"], 0)


if __name__ == "__main__":
    unittest.main()
