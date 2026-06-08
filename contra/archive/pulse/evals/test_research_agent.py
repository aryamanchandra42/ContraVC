"""
PULSE Research Agent Evals — V2 hardening invariant checks.

Four invariants enforced here (all run without a live LLM or network):

  1. Research provenance: entities_raw rows written by the research agent carry
     all required provenance columns (source_type='api', source_record_id is
     a valid 64-char hex string, content_hash is non-empty, source_offset starts
     with 'research:').

  2. Enrichment non-destructive: run_enrichment never overwrites a non-null
     allocator field (COALESCE-only invariant).

  3. Q&A SELECT-only validator: _validate_select_only rejects DDL/DML/multi-
     statement inputs, injects LIMIT when absent, caps excessive LIMITs.

  4. Review queue well-formedness: rows written to allocator_types.jsonl for
     low-confidence enrichments contain all required fields and valid evidence_pointers.

Run:
    pytest evals/test_research_agent.py -v
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Invariant 1: Research provenance in entities_raw
# ---------------------------------------------------------------------------

class TestResearchProvenance:
    """
    When the enrichment agent writes a raw provenance record it must satisfy
    all EntityRaw invariants without touching a live DB or LLM.
    We test the write helper in isolation.
    """

    def _make_in_memory_con(self):
        import duckdb
        con = duckdb.connect(":memory:")
        schema_sql = (ROOT / "schema" / "duckdb.sql").read_text(encoding="utf-8")
        try:
            con.execute(schema_sql)
        except Exception:
            # Minimal table if full DDL fails in isolation
            con.execute("""
                CREATE TABLE IF NOT EXISTS entities_raw (
                    source_record_id VARCHAR PRIMARY KEY,
                    source_file VARCHAR NOT NULL,
                    source_type VARCHAR NOT NULL,
                    source_offset VARCHAR NOT NULL,
                    content_hash VARCHAR NOT NULL,
                    raw_content JSON,
                    ingested_at TIMESTAMPTZ,
                    schema_version VARCHAR DEFAULT '1.0'
                )
            """)
        return con

    def test_raw_record_columns_present(self):
        """_write_research_raw_record inserts a row with all required provenance columns."""
        from agents.research.enrichment_agent import _write_research_raw_record
        con = self._make_in_memory_con()

        allocator_id = str(uuid.uuid4())
        payload = {
            "allocator_id": allocator_id,
            "canonical_name": "Test Capital Partners",
            "enrichment_result": {"allocator_type": {"value": "family_office_single", "confidence": 0.85}},
        }
        src_id = _write_research_raw_record(con, allocator_id, 0, payload)

        row = con.execute(
            "SELECT source_type, source_offset, content_hash, schema_version "
            "FROM entities_raw WHERE source_record_id = ?",
            [src_id],
        ).fetchone()

        assert row is not None, "No row was inserted into entities_raw"
        source_type, source_offset, content_hash, schema_version = row
        assert source_type == "api", f"Expected source_type='api', got '{source_type}'"
        assert source_offset.startswith("research:"), (
            f"source_offset must start with 'research:', got '{source_offset}'"
        )
        assert content_hash and len(content_hash) == 64, (
            f"content_hash must be a 64-char SHA-256 hex string, got '{content_hash}'"
        )
        assert schema_version == "1.0"

    def test_source_record_id_is_deterministic(self):
        """Same payload + allocator_id → identical source_record_id on re-run (idempotency)."""
        from agents.research.enrichment_agent import _write_research_raw_record
        con = self._make_in_memory_con()

        allocator_id = str(uuid.uuid4())
        payload = {"canonical_name": "Determinism Test LP", "value": 42}

        id1 = _write_research_raw_record(con, allocator_id, 0, payload)
        id2 = _write_research_raw_record(con, allocator_id, 0, payload)

        assert id1 == id2, "source_record_id must be deterministic for same inputs"

        # Only one row should exist (idempotency)
        count = con.execute(
            "SELECT COUNT(*) FROM entities_raw WHERE source_record_id = ?",
            [id1],
        ).fetchone()[0]
        assert count == 1, f"Expected 1 row (idempotent insert), got {count}"

    def test_source_record_id_format(self):
        """source_record_id is a 64-character lowercase hex string."""
        from agents.ingestion.base import make_source_record_id, hash_content

        content = {"test": "payload", "allocator": "ABC Capital"}
        ch = hash_content(content)
        src_id = make_source_record_id("research/enrichment/abc.json", "research:abc:0", ch)

        assert len(src_id) == 64, f"Expected 64-char hex, got len={len(src_id)}"
        assert re.match(r"^[0-9a-f]{64}$", src_id), f"Not a valid hex string: {src_id}"


# ---------------------------------------------------------------------------
# Invariant 2: Enrichment is non-destructive (COALESCE-only)
# ---------------------------------------------------------------------------

class TestEnrichmentNonDestructive:
    """
    _apply_enrichment_to_allocator must never overwrite an existing non-null value.
    """

    def _setup_allocator(self, con, allocator_id: str, alloc_type: str, geography: Optional[str]):
        con.execute(
            """
            INSERT INTO allocators (
                allocator_id, canonical_name, allocator_type, geography,
                source_record_id, source_file, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                allocator_id, "Test LP", alloc_type, geography,
                "fakesrc", "research/test.json", "fakehash",
            ],
        )

    def _make_in_memory_con(self):
        import duckdb
        con = duckdb.connect(":memory:")
        try:
            con.execute((ROOT / "schema" / "duckdb.sql").read_text(encoding="utf-8"))
        except Exception:
            con.execute("""
                CREATE TABLE IF NOT EXISTS allocators (
                    allocator_id VARCHAR PRIMARY KEY,
                    canonical_name VARCHAR,
                    allocator_type VARCHAR,
                    geography VARCHAR,
                    hq_country VARCHAR,
                    em_appetite VARCHAR,
                    ai_appetite VARCHAR,
                    stage_preference VARCHAR,
                    source_record_id VARCHAR,
                    source_file VARCHAR,
                    content_hash VARCHAR,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
        return con

    def test_does_not_overwrite_existing_type(self):
        """Existing allocator_type is preserved when enrichment proposes a different value."""
        from agents.research.enrichment_agent import _apply_enrichment_to_allocator
        con = self._make_in_memory_con()
        alloc_id = str(uuid.uuid4())
        self._setup_allocator(con, alloc_id, "fund_of_funds", None)

        updates = {"allocator_type": "family_office_single"}
        _apply_enrichment_to_allocator(con, alloc_id, updates)

        row = con.execute(
            "SELECT allocator_type FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
            [alloc_id],
        ).fetchone()
        assert row[0] == "fund_of_funds", (
            f"Existing allocator_type should be preserved; got '{row[0]}'"
        )

    def test_fills_null_field(self):
        """A NULL geography is filled by enrichment."""
        from agents.research.enrichment_agent import _apply_enrichment_to_allocator
        con = self._make_in_memory_con()
        alloc_id = str(uuid.uuid4())
        self._setup_allocator(con, alloc_id, "unknown", None)

        updates = {"geography": "southeast_asia"}
        cols = _apply_enrichment_to_allocator(con, alloc_id, updates)
        assert cols == 1, f"Expected 1 column updated, got {cols}"

        row = con.execute(
            "SELECT geography FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
            [alloc_id],
        ).fetchone()
        assert row[0] == "southeast_asia"

    def test_unknown_value_not_written(self):
        """'unknown' values are never written (they are non-informative)."""
        from agents.research.enrichment_agent import _apply_enrichment_to_allocator
        con = self._make_in_memory_con()
        alloc_id = str(uuid.uuid4())
        self._setup_allocator(con, alloc_id, "unknown", None)

        updates = {"allocator_type": "unknown", "geography": "unknown"}
        cols = _apply_enrichment_to_allocator(con, alloc_id, updates)
        assert cols == 0, f"'unknown' values should not be written; cols_updated={cols}"

    def test_only_enrichable_columns_allowed(self):
        """source_record_id / content_hash / ingested_at cannot be updated via enrichment."""
        from agents.research.enrichment_agent import _apply_enrichment_to_allocator
        con = self._make_in_memory_con()
        alloc_id = str(uuid.uuid4())
        self._setup_allocator(con, alloc_id, "unknown", None)

        # Attempt to inject provenance-column updates
        updates = {
            "source_record_id": "INJECTED",
            "content_hash": "INJECTED",
            "allocator_type": "asset_manager",
        }
        cols = _apply_enrichment_to_allocator(con, alloc_id, updates)

        # Only allocator_type should have been written (NULL → non-null)
        assert cols <= 1, f"Only enrichable columns should be writable; got {cols}"
        row = con.execute(
            "SELECT source_record_id FROM allocators WHERE CAST(allocator_id AS VARCHAR) = ?",
            [alloc_id],
        ).fetchone()
        assert row[0] == "fakesrc", "source_record_id must not be overwritten by enrichment"


# ---------------------------------------------------------------------------
# Invariant 3: Q&A SELECT-only validator
# ---------------------------------------------------------------------------

class TestQAValidator:

    def _validate(self, sql: str):
        from agents.research.qa_agent import _validate_select_only
        return _validate_select_only(sql)

    def test_accepts_valid_select(self):
        sql = "SELECT canonical_name, tier FROM icp_scores WHERE tier = 'tier_1'"
        clean, err = self._validate(sql)
        assert err is None, f"Valid SELECT should pass: {err}"
        assert "LIMIT" in clean.upper(), "LIMIT should be injected when absent"

    def test_accepts_select_with_limit(self):
        sql = "SELECT * FROM allocators_effective LIMIT 50"
        clean, err = self._validate(sql)
        assert err is None
        assert "LIMIT 50" in clean

    def test_rejects_insert(self):
        sql = "INSERT INTO allocators (canonical_name) VALUES ('Evil LP')"
        _, err = self._validate(sql)
        assert err is not None, "INSERT must be rejected"

    def test_rejects_update(self):
        sql = "UPDATE allocators SET allocator_type = 'hacked' WHERE 1=1"
        _, err = self._validate(sql)
        assert err is not None, "UPDATE must be rejected"

    def test_rejects_drop(self):
        sql = "DROP TABLE allocators"
        _, err = self._validate(sql)
        assert err is not None, "DROP must be rejected"

    def test_rejects_delete(self):
        sql = "DELETE FROM relationships"
        _, err = self._validate(sql)
        assert err is not None, "DELETE must be rejected"

    def test_rejects_multi_statement(self):
        sql = "SELECT 1; DROP TABLE allocators"
        _, err = self._validate(sql)
        assert err is not None, "Multi-statement SQL must be rejected"

    def test_caps_excessive_limit(self):
        sql = "SELECT * FROM allocators LIMIT 99999"
        clean, err = self._validate(sql)
        assert err is None
        limit_match = re.search(r"LIMIT\s+(\d+)", clean, re.IGNORECASE)
        assert limit_match is not None
        assert int(limit_match.group(1)) <= 500, "Excessive LIMIT must be capped at 500"

    def test_strips_trailing_semicolon(self):
        sql = "SELECT * FROM allocators_effective;"
        clean, err = self._validate(sql)
        assert err is None, f"Trailing semicolon should be stripped: {err}"

    def test_rejects_create_table(self):
        sql = "CREATE TABLE evil (id INTEGER)"
        _, err = self._validate(sql)
        assert err is not None, "CREATE TABLE must be rejected"

    def test_rejects_pragma(self):
        sql = "PRAGMA database_list"
        _, err = self._validate(sql)
        assert err is not None, "PRAGMA must be rejected"


# ---------------------------------------------------------------------------
# Invariant 4: Review queue row well-formedness
# ---------------------------------------------------------------------------

class TestReviewQueueWellFormedness:
    """
    Rows appended to allocator_types.jsonl for low-confidence enrichments
    must contain all required fields and valid evidence_pointers.
    """

    REQUIRED_FIELDS = {
        "queue_item_id", "target_type", "entity_id", "current_value",
        "evidence_pointers", "confidence", "reason", "metadata",
        "surfaced_at", "status",
    }

    def _read_queue_file(self, tmp_path: Path) -> list:
        queue_file = tmp_path / "allocator_types.jsonl"
        if not queue_file.exists():
            return []
        return [json.loads(line) for line in queue_file.read_text().splitlines() if line.strip()]

    def test_write_to_queue_produces_valid_row(self, tmp_path):
        """write_to_queue produces a row with all required fields."""
        import agents.reviews.queue_writer as qw
        original_dir = qw.QUEUES_DIR
        qw.QUEUES_DIR = tmp_path  # redirect writes to temp directory

        try:
            item_id = qw.write_to_queue(
                target_type="allocator_types",
                entity_id=str(uuid.uuid4()),
                current_value={"proposed_type": "family_office_single", "confidence": 0.38},
                evidence_pointers=[
                    {"source_file": "research/enrichment/abc.json", "source_record_id": "abcdef"}
                ],
                confidence=0.38,
                reason="llm_research_low_confidence (0.38 < 0.40)",
                metadata={"canonical_name": "ABC Capital"},
            )
        finally:
            qw.QUEUES_DIR = original_dir

        rows = self._read_queue_file(tmp_path)
        assert len(rows) == 1, f"Expected 1 row in queue, got {len(rows)}"
        row = rows[0]

        missing = self.REQUIRED_FIELDS - set(row.keys())
        assert not missing, f"Queue row missing required fields: {missing}"

        assert row["target_type"] == "allocator_types"
        assert row["status"] == "pending"
        assert isinstance(row["evidence_pointers"], list)
        assert len(row["evidence_pointers"]) > 0, "evidence_pointers must not be empty"
        assert "source_record_id" in row["evidence_pointers"][0] or "source_file" in row["evidence_pointers"][0]

    def test_invalid_target_type_raises(self):
        """write_to_queue raises ValueError for unknown target_type."""
        from agents.reviews.queue_writer import write_to_queue
        with pytest.raises(ValueError, match="Invalid target_type"):
            write_to_queue(
                target_type="INVALID_TYPE",
                entity_id="abc",
                current_value={},
                evidence_pointers=[],
                confidence=0.5,
                reason="test",
            )

    def test_confidence_below_threshold_triggers_queue(self):
        """should_queue returns True for confidence below low_confidence_threshold."""
        from agents.reviews.queue_writer import should_queue
        flag, reason = should_queue(
            confidence=0.25,
            contradiction_score=0.0,
            source_agreement_score=1.0,
            evidence_count=1,
            thresholds={"low_confidence_threshold": 0.40},
        )
        assert flag is True, "Low confidence should trigger queuing"
        assert "low_confidence" in reason

    def test_high_confidence_does_not_queue(self):
        """should_queue returns False when all indicators are good."""
        from agents.reviews.queue_writer import should_queue
        flag, _ = should_queue(
            confidence=0.90,
            contradiction_score=0.05,
            source_agreement_score=0.95,
            evidence_count=5,
            thresholds={
                "low_confidence_threshold": 0.40,
                "high_contradiction_threshold": 0.30,
                "low_source_agreement_threshold": 0.50,
            },
        )
        assert flag is False


# ---------------------------------------------------------------------------
# Bonus: LLM client factory raises LLMUnavailable when no provider set
# ---------------------------------------------------------------------------

class TestLLMClientFallback:

    def test_raises_when_no_provider(self, monkeypatch):
        """get_llm_client raises LLMUnavailable when PULSE_LLM_PROVIDER is 'none'."""
        monkeypatch.setenv("PULSE_LLM_PROVIDER", "none")
        from agents.research.llm_client import get_llm_client, LLMUnavailable
        with pytest.raises(LLMUnavailable):
            get_llm_client()

    def test_raises_when_missing_api_key(self, monkeypatch):
        """get_llm_client raises LLMUnavailable when API key is not set."""
        monkeypatch.setenv("PULSE_LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from agents.research.llm_client import get_llm_client, LLMUnavailable
        with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
            get_llm_client()

    def test_search_unavailable_when_no_provider(self, monkeypatch):
        """get_search_provider raises SearchUnavailable when PULSE_SEARCH_PROVIDER is 'none'."""
        monkeypatch.setenv("PULSE_SEARCH_PROVIDER", "none")
        from agents.research.web_search import get_search_provider, SearchUnavailable
        with pytest.raises(SearchUnavailable):
            get_search_provider()

    def test_groq_raises_when_missing_api_key(self, monkeypatch):
        """get_llm_client raises LLMUnavailable when GROQ_API_KEY is not set."""
        monkeypatch.setenv("PULSE_LLM_PROVIDER", "groq")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        from agents.research.llm_client import get_llm_client, LLMUnavailable
        with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
            get_llm_client()

    def test_unknown_provider_raises(self, monkeypatch):
        """get_llm_client raises LLMUnavailable for unrecognised provider names."""
        monkeypatch.setenv("PULSE_LLM_PROVIDER", "mistral_xyz")
        from agents.research.llm_client import get_llm_client, LLMUnavailable
        with pytest.raises(LLMUnavailable, match="Unknown LLM provider"):
            get_llm_client()


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
