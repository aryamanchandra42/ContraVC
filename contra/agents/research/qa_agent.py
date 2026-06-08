"""
PULSE Analyst Q&A Agent — natural-language → SQL → result → narrative.

Workflow:
  1. Receive a natural-language question from the analyst.
  2. Pass a compact schema card + question to the LLM → raw SQL string.
  3. Validate the SQL:
       - Must be a single SELECT statement (no DDL, DML, PRAGMA, semi-colons).
       - Automatically inject LIMIT if absent (safety cap).
       - Table/view references checked against the allowed whitelist.
  4. Execute against a read-only DuckDB connection.
  5. Return a QAAnswer with generated_sql + rows + narrative synthesis.

Graceful degradation:
  - If no LLM is configured: raise LLMUnavailable (caller shows error).
  - If SQL validation fails: return QAAnswer with error message, no DB call.
  - If query returns 0 rows: narrative says "no results found".
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema card — compact column reference for the LLM
# ---------------------------------------------------------------------------

_SCHEMA_CARD = """
Available tables / views (use _effective views for production queries):

allocators_effective
  allocator_id (UUID), canonical_name (TEXT), allocator_type (TEXT),
  geography (TEXT), hq_country (TEXT), em_appetite (TEXT), ai_appetite (TEXT),
  stage_preference (TEXT), check_size_min_usd (DOUBLE), check_size_max_usd (DOUBLE),
  check_size_bucket (TEXT), population (TEXT), relationship_density (DOUBLE),
  institutional_flexibility (TEXT), review_decision (TEXT)

icp_scores
  score_id (UUID), allocator_id (UUID), icp_version (TEXT),
  c1_asset_class_pass (BOOLEAN), c2_emerging_manager_pass (BOOLEAN),
  c3_ai_tech_pass (BOOLEAN), c4_geography_pass (BOOLEAN), core_pass (BOOLEAN),
  excluded (BOOLEAN), exclusion_reason (TEXT),
  s1_ai_signal (DOUBLE), s2_em_signal (DOUBLE), s3_lp_type (DOUBLE),
  s4_stage_pref (DOUBLE), s5_geo_overlap (DOUBLE), s6_network_density (DOUBLE),
  s7_proxy_fund (DOUBLE), fit_score (DOUBLE), tier (TEXT),
  client_status (TEXT), client_decision (TEXT), stated_reason (TEXT),
  c1_evidence (TEXT), c2_evidence (TEXT), c3_evidence (TEXT), c4_evidence (TEXT),
  scored_at (TIMESTAMP)

relationships_effective
  edge_id (UUID), source_node_id (TEXT), target_node_id (TEXT),
  edge_type (TEXT), weight (DOUBLE), confidence (DOUBLE),
  evidence_count (INT), temporal_confidence (DOUBLE),
  relationship_decay_score (DOUBLE), last_active (TIMESTAMP),
  first_seen (TIMESTAMP), last_seen (TIMESTAMP), review_decision (TEXT)

signals
  signal_id (UUID), allocator_id (UUID), signal_type (TEXT),
  raw_value (TEXT), normalized_value (DOUBLE), confidence (DOUBLE),
  evidence_count (INT), source_file (TEXT), ingested_at (TIMESTAMP)

funds
  fund_id (UUID), canonical_name (TEXT), fund_type (TEXT),
  manager_name (TEXT), vintage_year (INT), geography_focus (TEXT),
  strategy (TEXT), target_size_usd (DOUBLE), close_size_usd (DOUBLE)

investments
  investment_id (UUID), lp_id (UUID), fund_id (UUID),
  investment_date (DATE), commitment_usd (DOUBLE),
  syndicate_overlap (BOOLEAN), co_investment_flag (BOOLEAN)

benchmark_rankings
  ranking_id (UUID), allocator_id (UUID), rank_position (INT),
  source_list (TEXT), source_file (TEXT), ingested_at (TIMESTAMP)

entity_aliases
  alias_id (UUID), canonical_id (TEXT), entity_type (TEXT),
  alias_text (TEXT), confidence (DOUBLE), resolver_method (TEXT)

Signal types: response_speed, exploratory_check, operator_background,
  em_participation, geography_overlap, social_proximity, network_density,
  deployment_velocity

Edge types: invested_with, introduced_by, co_invested, syndicate_overlap,
  mutual_connection, repeated_exposure, co_mentioned, cross_file_corroboration

Allocator types: pension_fund, sovereign_wealth, endowment, foundation,
  family_office_single, family_office_multi, fund_of_funds, insurance, bank,
  asset_manager, development_finance, corporate, high_net_worth, angel, unknown

ICP tiers: tier_1 (strong fit), tier_2 (moderate), tier_3 (weak), tier_4 (excluded)

DuckDB dialect:
  - CAST(uuid_col AS VARCHAR) for UUID comparisons
  - strftime('%Y', timestamp_col) for year extraction
  - Use ILIKE for case-insensitive text search
"""

# ---------------------------------------------------------------------------
# SQL safety validator
# ---------------------------------------------------------------------------

# Tables/views the agent is allowed to reference
_ALLOWED_TABLES = frozenset({
    "allocators", "allocators_effective",
    "icp_scores",
    "relationships", "relationships_effective",
    "signals",
    "funds",
    "investments",
    "benchmark_rankings",
    "entity_aliases",
    "human_reviews",
    "ontology_terms", "ontology_terms_effective",
    "relationship_evidence",
    "evidence_summary",
    "relationship_decay_view",
    "calibration_overlay",
})

_DDL_DML_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|EXEC|EXECUTE|PRAGMA)\b",
    re.IGNORECASE,
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


def _validate_select_only(sql: str) -> Tuple[str, Optional[str]]:
    """
    Validate that sql is a safe read-only SELECT.

    Returns (cleaned_sql, error_message_or_None).
    Injects a LIMIT if absent; caps existing LIMIT at _MAX_LIMIT.
    """
    stripped = sql.strip().rstrip(";")

    # Reject if contains multiple statements (semicolons in body)
    if ";" in stripped:
        return "", "SQL contains multiple statements (semicolon). Only single SELECT allowed."

    if not stripped.upper().lstrip().startswith("SELECT"):
        return "", f"SQL must start with SELECT. Got: {stripped[:40]}..."

    if _DDL_DML_PATTERN.search(stripped):
        m = _DDL_DML_PATTERN.search(stripped)
        return "", f"SQL contains disallowed keyword: {m.group()}."

    # Inject LIMIT if absent
    limit_match = re.search(r"\bLIMIT\s+(\d+)\b", stripped, re.IGNORECASE)
    if limit_match:
        existing_limit = int(limit_match.group(1))
        if existing_limit > _MAX_LIMIT:
            stripped = re.sub(
                r"\bLIMIT\s+\d+\b",
                f"LIMIT {_MAX_LIMIT}",
                stripped,
                flags=re.IGNORECASE,
            )
    else:
        stripped = f"{stripped}\nLIMIT {_DEFAULT_LIMIT}"

    return stripped, None


def _extract_cited_tables(sql: str) -> List[str]:
    """Best-effort extraction of table/view names referenced in SQL."""
    from_pattern = re.finditer(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z_0-9]*)",
        sql,
        re.IGNORECASE,
    )
    tables = []
    for m in from_pattern:
        name = m.group(1).lower()
        if name in _ALLOWED_TABLES:
            tables.append(name)
    return list(dict.fromkeys(tables))  # dedupe, preserve order


# Re-export for type annotation without circular import
from typing import Tuple  # noqa: E402


# ---------------------------------------------------------------------------
# Narrative synthesis prompt
# ---------------------------------------------------------------------------

def _build_narrative_prompt(question: str, sql: str, rows: List[Dict[str, Any]]) -> str:
    rows_preview = rows[:20]  # don't send thousands of rows to LLM
    rows_text = "\n".join(str(r) for r in rows_preview)
    if len(rows) > 20:
        rows_text += f"\n... ({len(rows) - 20} more rows not shown)"

    return f"""You are a private-market analyst. Answer the user's question concisely.

Question: {question}

SQL executed:
{sql}

Query result ({len(rows)} rows):
{rows_text if rows_text.strip() else "(no rows returned)"}

Instructions:
- Write 1–3 sentences answering the question directly.
- Reference specific numbers or entity names from the result.
- If 0 rows: say "No results found for this query."
- Do NOT hallucinate data not in the result.
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ask(
    con,
    question: str,
    max_rows: int = _DEFAULT_LIMIT,
) -> "QAAnswer":  # type: ignore[name-defined]  # forward ref resolved at runtime
    """
    Answer a natural-language question over the PULSE database.

    Parameters
    ----------
    con : DuckDB connection (should be read-only)
    question : free-form analyst question in English
    max_rows : maximum result rows to return

    Returns
    -------
    QAAnswer with generated_sql, rows, row_count, narrative, cited_tables, confidence
    """
    from agents.research.schemas import QAAnswer
    from agents.research.llm_client import get_llm_client, LLMUnavailable, LLMExtractionError

    # --- Get LLM (required for Q&A) ---
    try:
        llm_client = get_llm_client()
    except LLMUnavailable as exc:
        logger.warning("LLM unavailable for Q&A: %s", exc)
        return QAAnswer(
            generated_sql="-- LLM unavailable",
            rows=[],
            row_count=0,
            narrative=f"Q&A requires a configured LLM provider. {exc}",
            cited_tables=[],
            confidence=0.0,
        )

    # --- Step 1: generate SQL via LLM ---
    sql_prompt = f"""You are a DuckDB SQL expert. Generate a single SELECT query to answer the question below.

SCHEMA:
{_SCHEMA_CARD}

RULES:
- Return ONLY the SQL SELECT statement, no explanation, no markdown.
- Always use _effective views (allocators_effective, relationships_effective, ontology_terms_effective).
- Cast UUID columns with CAST(col AS VARCHAR) for string comparisons.
- Include a LIMIT clause (max {min(max_rows, _MAX_LIMIT)}).
- Use ILIKE for case-insensitive text matching.

QUESTION: {question}

SQL:"""

    try:
        # We want a raw string, not a Pydantic model here — use a simple wrapper
        raw_sql_result = llm_client.structured(
            prompt=sql_prompt,
            response_model=_SQLOnlyResponse,
            system="Return only valid DuckDB SQL. No explanation. No markdown fences.",
        )
        raw_sql = raw_sql_result.sql.strip()
    except (LLMExtractionError, Exception) as exc:
        logger.error("SQL generation failed: %s", exc)
        return QAAnswer(
            generated_sql="-- SQL generation failed",
            rows=[],
            row_count=0,
            narrative=f"Failed to generate SQL: {exc}",
            cited_tables=[],
            confidence=0.0,
        )

    # --- Step 2: validate SQL ---
    safe_sql, err = _validate_select_only(raw_sql)
    if err:
        logger.warning("SQL validation failed: %s\nSQL: %s", err, raw_sql)
        return QAAnswer(
            generated_sql=raw_sql,
            rows=[],
            row_count=0,
            narrative=f"Generated SQL failed safety validation: {err}",
            cited_tables=[],
            confidence=0.0,
        )

    cited_tables = _extract_cited_tables(safe_sql)

    # --- Step 3: execute ---
    try:
        df = con.execute(safe_sql).df()
        rows = df.head(max_rows).to_dict(orient="records")
        row_count = len(df)
    except Exception as exc:
        logger.error("SQL execution failed: %s\nSQL: %s", exc, safe_sql)
        return QAAnswer(
            generated_sql=safe_sql,
            rows=[],
            row_count=0,
            narrative=f"SQL execution error: {exc}",
            cited_tables=cited_tables,
            confidence=0.3,
        )

    # --- Step 4: synthesize narrative ---
    try:
        narrative_result = llm_client.structured(
            prompt=_build_narrative_prompt(question, safe_sql, rows),
            response_model=_NarrativeResponse,
            system="You are a private-market analyst. Answer concisely and accurately.",
        )
        narrative = narrative_result.narrative
        confidence = narrative_result.confidence
    except Exception as exc:
        logger.warning("Narrative synthesis failed (non-fatal): %s", exc)
        narrative = f"Query returned {row_count} rows. (Narrative synthesis unavailable.)"
        confidence = 0.7

    logger.info("Q&A complete: '%s' → %d rows", question[:60], row_count)

    return QAAnswer(
        generated_sql=safe_sql,
        rows=rows,
        row_count=row_count,
        narrative=narrative,
        cited_tables=cited_tables,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Private Pydantic helpers for LLM extraction
# ---------------------------------------------------------------------------

from pydantic import BaseModel, ConfigDict  # noqa: E402
from typing import Annotated  # noqa: E402
from pydantic import Field  # noqa: E402

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class _SQLOnlyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(description="A single valid DuckDB SELECT statement, no markdown.")


class _NarrativeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narrative: str = Field(description="1–3 sentence answer to the analyst's question.")
    confidence: Probability = Field(
        0.8,
        description="Confidence that the narrative accurately answers the question.",
    )
