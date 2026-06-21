"""
Contact intelligence tests — gate extraction, channel recommendation.

Ihar Mahniok fixture: gate research found his personal email, LinkedIn and X.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from contra.intelligence.channel_recommend import recommend_channel
from contra.intelligence.contact_extract import (
    _extract_from_text,
    _extract_from_analyst_facts,
    extract_and_persist_gate_contacts,
)
from contra.intelligence.contact_resolver import ContactProfile, ContactChannel, ContactPerson


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


# ---------------------------------------------------------------------------
# Channel recommendation
# ---------------------------------------------------------------------------

class TestChannelRecommend(unittest.TestCase):

    def _profile_with(self, *, email=None, linkedin=None, twitter=None) -> ContactProfile:
        channels = []
        if email:
            channels.append(ContactChannel(type="email", value=email, source="gate_research", confidence=0.9))
        if linkedin:
            channels.append(ContactChannel(type="linkedin", value=linkedin, source="gate_research", confidence=0.8))
        if twitter:
            channels.append(ContactChannel(type="twitter", value=twitter, source="gate_research", confidence=0.75))
        person = ContactPerson(full_name="Test LP", title=None, company=None, location=None, channels=channels)
        return ContactProfile(
            investor_name="Test LP",
            allocator_id="alloc-001",
            contacts=[person],
        )

    def test_personal_domain_email_recommended(self):
        profile = self._profile_with(
            email="i@mahaniok.com",
            linkedin="https://linkedin.com/in/imahaniok",
            twitter="https://x.com/imahaniok",
        )
        result = recommend_channel(profile, warm_path_count=0, investor_type="individual")
        self.assertEqual(result.recommended_channel, "email")
        # Custom domain → personal domain heuristic
        self.assertIn("mahaniok", result.recommendation_rationale.lower())

    def test_warm_intro_always_wins(self):
        profile = self._profile_with(email="cio@largefund.com")
        result = recommend_channel(profile, warm_path_count=3, investor_type="family_office")
        self.assertEqual(result.recommended_channel, "warm_intro")
        self.assertEqual(result.confidence, 0.95)

    def test_linkedin_only(self):
        profile = self._profile_with(linkedin="https://linkedin.com/in/someone")
        result = recommend_channel(profile, warm_path_count=0)
        self.assertEqual(result.recommended_channel, "linkedin")

    def test_twitter_only(self):
        profile = self._profile_with(twitter="https://x.com/angelinvestor")
        result = recommend_channel(profile, warm_path_count=0)
        self.assertEqual(result.recommended_channel, "twitter")
        self.assertLess(result.confidence, 0.5)

    def test_no_channels_empty_recommendation(self):
        profile = ContactProfile(investor_name="Unknown LP", allocator_id=None)
        result = recommend_channel(profile)
        self.assertEqual(result.recommended_channel, "")
        self.assertEqual(result.confidence, 0.0)

    def test_work_email_preferred_for_institutional(self):
        profile = self._profile_with(email="partner@vcfirm.com", linkedin="https://linkedin.com/in/someone")
        result = recommend_channel(profile, warm_path_count=0, investor_type="family_office")
        self.assertEqual(result.recommended_channel, "email")
        self.assertGreater(result.confidence, 0.8)


# ---------------------------------------------------------------------------
# Phantombuster sync (mocked HTTP)
# ---------------------------------------------------------------------------

class TestPhantombusterSync(unittest.TestCase):

    def test_sync_normalizes_rows_and_calls_enrichment(self):
        """Verify sync wires through: launch → poll → fetch → normalize → persist → enrich."""
        from agents.ingestion.phantombuster_sync import run_phantombuster_sync

        mock_rows = [
            {
                "firstName": "Ihar",
                "lastName": "Mahniok",
                "title": "Investor",
                "company": "Personal",
                "email": "i@mahaniok.com",
                "profileUrl": "https://www.linkedin.com/in/imahaniok",
                "location": "Minsk, Belarus",
            }
        ]

        con = MagicMock()
        con.execute.return_value.fetchall.return_value = []
        con.execute.return_value.fetchone.return_value = None

        with patch("agents.ingestion.phantombuster_sync.launch", return_value="cid-123"), \
             patch("agents.ingestion.phantombuster_sync.poll_until_done", return_value={"status": "finished"}), \
             patch("agents.ingestion.phantombuster_sync.fetch_result_rows", return_value=mock_rows), \
             patch("agents.ingestion.phantombuster_sync.persist_raw_records", return_value=1) as mock_persist, \
             patch("agents.normalization.linkedin_enricher.run_linkedin_enrichment", return_value={"matched": 0, "aliases_created": 0}) as mock_enrich:

            stats = run_phantombuster_sync(con, agent_id="agent-456", save_csv=False)

        self.assertEqual(stats["container_id"], "cid-123")
        self.assertEqual(stats["rows_fetched"], 1)
        self.assertEqual(stats["rows_inserted"], 1)
        mock_persist.assert_called_once()
        mock_enrich.assert_called_once()

    def test_sync_raises_on_missing_api_key(self):
        import os
        from agents.ingestion.phantombuster_client import PhantombusterError, _api_key

        original = os.environ.pop("PHANTOMBUSTER_API_KEY", None)
        try:
            with self.assertRaises(PhantombusterError):
                _api_key()
        finally:
            if original:
                os.environ["PHANTOMBUSTER_API_KEY"] = original


if __name__ == "__main__":
    unittest.main()
