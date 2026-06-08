-- PULSE SQL Views — Derived views for uncertainty rollups, temporal decay, and human-review application.
-- Compatible with both DuckDB and Postgres (with minor notes).
-- These views are the canonical query surface for production code and notebooks.
-- NEVER query relationships/allocators/ontology_terms directly — use the _effective views.

-- ============================================================
-- Helper: latest non-superseded review per entity
-- ============================================================
-- Returns the most recent human_reviews row per entity that has not been superseded.

CREATE OR REPLACE VIEW latest_reviews AS
SELECT hr.*
FROM human_reviews hr
WHERE NOT EXISTS (
    SELECT 1 FROM human_reviews hr2
    WHERE hr2.supersedes = hr.review_id
);

-- ============================================================
-- relationships_effective
-- Applies human review overrides to relationships.
-- decision='reject'  → excluded from this view
-- decision='revise'  → confidence adjusted, weight from override_payload if present
-- decision='confirm' → confidence_adjustment applied
-- decision='defer'   → no change (row still visible)
-- ============================================================

CREATE OR REPLACE VIEW relationships_effective AS
SELECT
    r.edge_id,
    r.source_node_id,
    r.source_node_type,
    r.target_node_id,
    r.target_node_type,
    r.edge_type,
    CASE
        WHEN lr.decision = 'revise' AND lr.override_payload IS NOT NULL
            THEN COALESCE(CAST(JSON_EXTRACT_STRING(lr.override_payload, '$.weight') AS DOUBLE), r.weight)
        ELSE r.weight
    END AS weight,
    r.effective_date,
    r.first_seen,
    r.last_seen,
    r.last_active,
    r.relationship_decay_score,
    r.temporal_confidence,
    CASE
        WHEN lr.decision IN ('confirm', 'revise') AND lr.confidence_adjustment IS NOT NULL
            THEN LEAST(1.0, GREATEST(0.0, r.confidence + lr.confidence_adjustment))
        ELSE r.confidence
    END AS confidence,
    r.evidence_count,
    r.contradiction_score,
    r.source_agreement_score,
    r.created_at,
    r.updated_at,
    lr.review_id           AS review_id,
    lr.decision            AS review_decision,
    lr.override_payload    AS review_override,
    lr.reviewer            AS reviewer
FROM relationships r
LEFT JOIN latest_reviews lr
    ON lr.entity_id = CAST(r.edge_id AS VARCHAR)
    AND lr.target_type = 'relationship_edge'
WHERE
    lr.decision IS NULL OR lr.decision != 'reject';

-- ============================================================
-- allocators_effective
-- Applies allocator_archetype overrides.
-- ============================================================

CREATE OR REPLACE VIEW allocators_effective AS
SELECT
    a.allocator_id,
    a.canonical_name,
    a.aliases,
    CASE
        WHEN lr.decision = 'revise' AND lr.override_payload IS NOT NULL
            THEN COALESCE(JSON_EXTRACT_STRING(lr.override_payload, '$.allocator_type'), a.allocator_type)
        ELSE a.allocator_type
    END AS allocator_type,
    a.geography,
    a.hq_country,
    a.stage_preference,
    a.check_size_min_usd,
    a.check_size_max_usd,
    a.check_size_bucket,
    a.em_appetite,
    a.ai_appetite,
    a.relationship_density,
    a.institutional_flexibility,
    a.inferred_scores,
    a.confidences,
    a.source_record_id,
    a.source_file,
    a.ingested_at,
    a.content_hash,
    a.created_at,
    a.updated_at,
    lr.review_id        AS review_id,
    lr.decision         AS review_decision,
    lr.reviewer         AS reviewer
FROM allocators a
LEFT JOIN latest_reviews lr
    ON lr.entity_id = CAST(a.allocator_id AS VARCHAR)
    AND lr.target_type = 'allocator_archetype';

-- ============================================================
-- ontology_terms_effective
-- Applies ontology label overrides.
-- ============================================================

CREATE OR REPLACE VIEW ontology_terms_effective AS
SELECT
    ot.term_id,
    CASE
        WHEN lr.decision = 'revise' AND lr.override_payload IS NOT NULL
            THEN COALESCE(JSON_EXTRACT_STRING(lr.override_payload, '$.term'), ot.term)
        ELSE ot.term
    END AS term,
    ot.category,
    CASE
        WHEN lr.decision = 'revise' AND lr.override_payload IS NOT NULL
            THEN COALESCE(JSON_EXTRACT_STRING(lr.override_payload, '$.canonical_label'), ot.canonical_label)
        ELSE ot.canonical_label
    END AS canonical_label,
    ot.description,
    CASE
        WHEN lr.decision IN ('confirm', 'revise') AND lr.confidence_adjustment IS NOT NULL
            THEN LEAST(1.0, GREATEST(0.0, ot.confidence + lr.confidence_adjustment))
        ELSE ot.confidence
    END AS confidence,
    ot.evidence_count,
    ot.contradiction_score,
    ot.source_agreement_score,
    ot.first_seen,
    ot.last_seen,
    lr.review_id    AS review_id,
    lr.decision     AS review_decision,
    lr.reviewer     AS reviewer
FROM ontology_terms ot
LEFT JOIN latest_reviews lr
    ON lr.entity_id = CAST(ot.term_id AS VARCHAR)
    AND lr.target_type = 'ontology_term'
WHERE
    lr.decision IS NULL OR lr.decision != 'reject';

-- ============================================================
-- relationship_decay_view
-- Materializes the temporal decay computation for analytics / debugging.
--
-- WARNING: The half-life constant below (365.0 days) MUST match
--   prompts/uncertainty.yaml → temporal.half_life_days
-- This view is read-only analytics. The AUTHORITATIVE decay scores written
-- to relationships.relationship_decay_score are computed by
-- agents/uncertainty/temporal.py which reads from uncertainty.yaml.
-- If you change half_life_days in the yaml, update 365.0 here in both
-- the DuckDB and Postgres versions, then re-run `pulse derive`.
--
-- DuckDB note: uses EPOCH for timestamp arithmetic.
-- Postgres note: uses EXTRACT(EPOCH FROM ...) / 86400 for days.
-- ============================================================

-- DuckDB version (half_life_days = 365 — sync with prompts/uncertainty.yaml):
CREATE OR REPLACE VIEW relationship_decay_view AS
SELECT
    r.edge_id,
    r.source_node_id,
    r.target_node_id,
    r.edge_type,
    r.last_active,
    r.confidence,
    r.evidence_count,
    r.temporal_confidence,
    CASE
        WHEN r.last_active IS NULL THEN NULL
        ELSE EXP(
            -1.0 * (
                EPOCH(NOW()) - EPOCH(r.last_active)
            ) / (86400.0 * 365.0)  -- SYNC: prompts/uncertainty.yaml half_life_days
        )
    END AS recomputed_decay_score,
    CASE
        WHEN r.last_active IS NULL OR r.confidence IS NULL THEN NULL
        ELSE r.confidence * EXP(
            -1.0 * (
                EPOCH(NOW()) - EPOCH(r.last_active)
            ) / (86400.0 * 365.0)  -- SYNC: prompts/uncertainty.yaml half_life_days
        )
    END AS recomputed_temporal_confidence
FROM relationships r;

-- ============================================================
-- evidence_summary
-- Aggregate view: one row per edge, showing evidence statistics.
-- Used by the aggregator to derive uncertainty columns.
-- ============================================================

CREATE OR REPLACE VIEW evidence_summary AS
SELECT
    re.edge_id,
    COUNT(*)                                        AS evidence_count,
    -- Noisy-OR confidence combinator: 1 - PRODUCT(1 - s*c)
    -- DuckDB supports EXP(SUM(LOG(...))) for product aggregation
    1.0 - EXP(SUM(LN(1.0 - LEAST(0.9999, re.evidence_strength * re.confidence))))
                                                    AS combined_confidence,
    COUNT(DISTINCT JSON_EXTRACT_STRING(re.provenance_pointer, '$.source_file'))
                                                    AS observing_source_count,
    COUNT(DISTINCT CASE WHEN re.evidence_type NOT LIKE 'contradicts%'
        THEN JSON_EXTRACT_STRING(re.provenance_pointer, '$.source_file')
        END)                                        AS agreeing_source_count,
    COUNT(DISTINCT CASE WHEN re.evidence_type LIKE 'contradicts%'
        THEN JSON_EXTRACT_STRING(re.provenance_pointer, '$.source_file')
        END)                                        AS contradicting_source_count,
    MIN(re.timestamp)                               AS first_seen,
    MAX(re.timestamp)                               AS last_seen
FROM relationship_evidence re
GROUP BY re.edge_id;

-- ============================================================
-- calibration_overlay
-- Join PULSE ICP scores to ContraVC benchmark for calibration analytics.
-- ============================================================

CREATE OR REPLACE VIEW calibration_overlay AS
SELECT
    CAST(a.allocator_id AS VARCHAR) AS allocator_id,
    a.canonical_name,
    a.population,
    i.tier AS pulse_tier,
    i.fit_score AS pulse_fit,
    i.client_decision,
    b.rank AS contra_rank,
    b.priority_score AS contra_priority,
    b.tier AS contra_tier,
    b.prior_fund_lp,
    CASE WHEN b.rank IS NOT NULL THEN TRUE ELSE FALSE END AS in_contra_top200
FROM icp_scores i
JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
LEFT JOIN benchmark_rankings b
    ON CAST(b.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
    AND b.ranking_source = 'contravc_top200'
WHERE i.icp_version = '4.1';
