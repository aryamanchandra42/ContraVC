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

-- ============================================================
-- Contra LLM navigation views
-- ============================================================

CREATE OR REPLACE VIEW v_document_chunks AS
SELECT
    source_record_id,
    source_file,
    source_type,
    COALESCE(
        json_extract_string(raw_content, '$.text'),
        json_extract_string(raw_content, 'text')
    ) AS chunk_text,
    json_extract_string(raw_content, '$._slide') AS chunk_index,
    content_hash,
    ingested_at
FROM entities_raw
WHERE source_type IN ('pdf', 'docx')
  AND COALESCE(
        json_extract_string(raw_content, '$.text'),
        json_extract_string(raw_content, 'text')
      ) IS NOT NULL
  AND length(COALESCE(
        json_extract_string(raw_content, '$.text'),
        json_extract_string(raw_content, 'text')
      )) > 30;

CREATE OR REPLACE VIEW v_crm_contacts AS
SELECT * FROM crm_contacts;

-- ============================================================
-- v_syndicate_profile — fund-LP behavior for syndicate roster
-- ============================================================

CREATE OR REPLACE VIEW v_syndicate_profile AS
SELECT
    CAST(a.allocator_id AS VARCHAR) AS allocator_id,
    a.canonical_name,
    a.geography,
    a.allocator_type,
    COUNT(inv.investment_id)                                        AS total_deal_count,
    COUNT(CASE WHEN lower(inv.notes) IN ('venture fund', 'fund') THEN 1 END)
                                                                    AS fund_deal_count,
    COUNT(CASE WHEN lower(inv.notes) = 'spv' THEN 1 END)           AS spv_deal_count,
    COALESCE(SUM(inv.commitment_usd), 0)                           AS total_committed_usd,
    CASE WHEN COUNT(inv.investment_id) > 0
         THEN ROUND(
             COUNT(CASE WHEN lower(inv.notes) IN ('venture fund', 'fund') THEN 1 END)::DOUBLE
             / COUNT(inv.investment_id), 3)
         ELSE 0.0
    END                                                             AS fund_lp_ratio,
    COUNT(CASE WHEN lower(inv.notes) IN ('venture fund', 'fund') THEN 1 END) >= 1
                                                                    AS is_fund_lp,
    (COUNT(CASE WHEN lower(inv.notes) IN ('venture fund', 'fund') THEN 1 END) >= 1
     AND COALESCE(SUM(inv.commitment_usd), 0) >= 5000)             AS is_upgrade_candidate,
    MAX(inv.investment_date)                                        AS last_investment_date,
    (
        EXISTS (
            SELECT 1 FROM crm_contacts c
            WHERE lower(regexp_replace(c.investor_name, '[^a-zA-Z0-9]', '', 'g'))
                = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
        )
        OR EXISTS (
            SELECT 1 FROM crm_leads l
            WHERE l.status != 'passed'
              AND (
                  l.name_key = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
                  OR lower(regexp_replace(l.investor_name, '[^a-zA-Z0-9]', '', 'g'))
                      = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
              )
        )
    )                                                               AS in_crm,
    -- top syndicate signals
    (SELECT normalized_value FROM signals s
     WHERE CAST(s.allocator_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
       AND s.signal_type = 'fund_lp_behavior'
     LIMIT 1)                                                       AS fund_lp_behavior_score,
    (SELECT normalized_value FROM signals s
     WHERE CAST(s.allocator_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
       AND s.signal_type = 'syndicate_depth'
     LIMIT 1)                                                       AS syndicate_depth_score
FROM allocators a
LEFT JOIN investments inv ON inv.lp_id = a.allocator_id
WHERE a.population = 'syndicate_lp'
GROUP BY
    CAST(a.allocator_id AS VARCHAR), a.canonical_name,
    a.geography, a.allocator_type;

-- ============================================================
-- v_warm_paths — human-readable mutual_connection intro routes
-- ============================================================

CREATE OR REPLACE VIEW v_warm_paths AS
SELECT
    CAST(prospect.allocator_id AS VARCHAR) AS prospect_id,
    prospect.canonical_name                AS prospect_name,
    CAST(bridge.allocator_id AS VARCHAR)   AS bridge_id,
    bridge.canonical_name                  AS bridge_name,
    bridge.allocator_type                  AS bridge_type,
    CAST(r.edge_id AS VARCHAR)             AS edge_id,
    r.weight                               AS bridge_strength,
    r.temporal_confidence,
    r.evidence_count,
    json_extract_string(re.provenance_pointer, '$.bridge_node_id')
                                           AS bridge_node_id_hint
FROM relationships_effective r
JOIN relationship_evidence re
    ON CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
   AND re.evidence_type = 'graph_path_inference'
JOIN allocators prospect
    ON CAST(prospect.allocator_id AS VARCHAR) = CAST(r.source_node_id AS VARCHAR)
   AND prospect.population = 'institutional_prospect'
JOIN allocators bridge
    ON CAST(bridge.allocator_id AS VARCHAR) = CAST(r.target_node_id AS VARCHAR)
WHERE r.edge_type = 'mutual_connection'
UNION ALL
SELECT
    CAST(prospect.allocator_id AS VARCHAR),
    prospect.canonical_name,
    CAST(bridge.allocator_id AS VARCHAR),
    bridge.canonical_name,
    bridge.allocator_type,
    CAST(r.edge_id AS VARCHAR),
    r.weight,
    r.temporal_confidence,
    r.evidence_count,
    json_extract_string(re.provenance_pointer, '$.bridge_node_id')
FROM relationships_effective r
JOIN relationship_evidence re
    ON CAST(re.edge_id AS VARCHAR) = CAST(r.edge_id AS VARCHAR)
   AND re.evidence_type = 'graph_path_inference'
JOIN allocators prospect
    ON CAST(prospect.allocator_id AS VARCHAR) = CAST(r.target_node_id AS VARCHAR)
   AND prospect.population = 'institutional_prospect'
JOIN allocators bridge
    ON CAST(bridge.allocator_id AS VARCHAR) = CAST(r.source_node_id AS VARCHAR)
WHERE r.edge_type = 'mutual_connection';

CREATE OR REPLACE VIEW v_lp_profile AS
SELECT
    CAST(a.allocator_id AS VARCHAR) AS allocator_id,
    a.canonical_name,
    a.allocator_type,
    a.geography,
    a.population,
    a.em_appetite,
    a.ai_appetite,
    a.check_size_bucket,
    i.tier AS icp_tier,
    i.fit_score,
    i.core_pass,
    i.excluded,
    i.exclusion_reason,
    i.client_decision,
    i.c1_evidence,
    i.c2_evidence,
    i.c3_evidence,
    i.c4_evidence,
    (
        EXISTS (
            SELECT 1 FROM crm_contacts c
            WHERE lower(regexp_replace(c.investor_name, '[^a-zA-Z0-9]', '', 'g'))
                = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
        )
        OR EXISTS (
            SELECT 1 FROM crm_leads l
            WHERE l.status != 'passed'
              AND (
                  l.name_key = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
                  OR lower(regexp_replace(l.investor_name, '[^a-zA-Z0-9]', '', 'g'))
                      = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
              )
        )
    ) AS in_crm,
    b.rank AS contra_rank,
    (SELECT COUNT(*) FROM investments inv WHERE inv.lp_id = a.allocator_id) AS investment_count,
    (SELECT COUNT(*) FROM signals s WHERE s.allocator_id = a.allocator_id) AS signal_count,
    (SELECT COUNT(*) FROM relationships_effective r
     WHERE r.edge_type = 'mutual_connection'
       AND (CAST(r.source_node_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
         OR CAST(r.target_node_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR))
    ) AS warm_path_count
FROM allocators a
LEFT JOIN icp_scores i
    ON CAST(i.allocator_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
   AND i.icp_version = '4.1'
LEFT JOIN benchmark_rankings b
    ON CAST(b.allocator_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
   AND b.ranking_source = 'contravc_top200';

-- ============================================================
-- CRM workspace views
-- ============================================================

CREATE OR REPLACE VIEW v_crm_workspace AS
SELECT
    l.lead_id,
    l.investor_name,
    l.name_key,
    l.allocator_id,
    l.source,
    l.status,
    l.investor_type,
    l.investor_location,
    l.investor_details,
    l.contacts_json,
    l.pipeline_stage,
    l.computed_score,
    l.manual_rank,
    COALESCE(
        l.manual_rank,
        CAST(ROW_NUMBER() OVER (ORDER BY l.computed_score DESC NULLS LAST) AS INTEGER)
    ) AS effective_rank,
    l.gate_session_id,
    l.gate_verdict,
    l.gate_confidence,
    l.gate_summary,
    l.icp_tier,
    l.fit_score,
    l.contra_rank,
    l.warm_path_count,
    l.syndicate_score,
    l.needs_enrichment,
    l.created_at,
    l.updated_at
FROM crm_leads l
WHERE l.status != 'passed';

CREATE OR REPLACE VIEW v_crm_prospects AS
SELECT * FROM (
    SELECT
        CAST(p.allocator_id AS VARCHAR) AS allocator_id,
        p.canonical_name AS investor_name,
        p.allocator_type AS investor_type,
        p.geography AS investor_location,
        p.icp_tier,
        p.fit_score,
        p.contra_rank,
        p.warm_path_count,
        COALESCE(
            (SELECT CAST(s.normalized_value AS DOUBLE) FROM signals s
             WHERE CAST(s.allocator_id AS VARCHAR) = CAST(p.allocator_id AS VARCHAR)
               AND s.signal_type = 'fund_lp_behavior' LIMIT 1),
            0.0
        ) AS syndicate_score,
        'icp' AS suggested_source,
        COALESCE(p.fit_score, 0) * 0.35
            + LEAST(COALESCE(p.warm_path_count, 0), 5) / 5.0 * 15.0
            + CASE WHEN p.contra_rank IS NOT NULL AND p.contra_rank <= 200
                   THEN (201 - p.contra_rank) / 200.0 * 15.0 ELSE 0 END
            + COALESCE(
                (SELECT CAST(s.normalized_value AS DOUBLE) FROM signals s
                 WHERE CAST(s.allocator_id AS VARCHAR) = CAST(p.allocator_id AS VARCHAR)
                   AND s.signal_type = 'fund_lp_behavior' LIMIT 1),
                0.0
              ) * 0.15 AS prospect_score
    FROM v_lp_profile p
    WHERE p.icp_tier IN ('tier_1', 'tier_2')
      AND NOT COALESCE(p.excluded, FALSE)
      AND NOT p.in_crm

    UNION ALL

    SELECT
        s.allocator_id,
        s.canonical_name,
        s.allocator_type,
        s.geography,
        NULL AS icp_tier,
        NULL AS fit_score,
        NULL AS contra_rank,
        NULL AS warm_path_count,
        COALESCE(s.fund_lp_behavior_score, 0.0) AS syndicate_score,
        'syndicate' AS suggested_source,
        COALESCE(s.fund_lp_behavior_score, 0) * 0.15
            + CASE WHEN s.is_upgrade_candidate THEN 50 ELSE 30 END AS prospect_score
    FROM v_syndicate_profile s
    WHERE s.is_upgrade_candidate AND NOT s.in_crm

    UNION ALL

    SELECT
        CAST(b.allocator_id AS VARCHAR),
        a.canonical_name,
        a.allocator_type,
        a.geography,
        NULL,
        NULL,
        b.rank,
        NULL,
        0.0,
        'benchmark',
        CASE WHEN b.rank IS NOT NULL THEN (201 - b.rank) / 200.0 * 100 ELSE 0 END
    FROM benchmark_rankings b
    JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(b.allocator_id AS VARCHAR)
    WHERE b.ranking_source = 'contravc_top200'
      AND b.rank <= 50
      AND NOT EXISTS (
          SELECT 1 FROM v_lp_profile p
          WHERE CAST(p.allocator_id AS VARCHAR) = CAST(b.allocator_id AS VARCHAR)
            AND p.in_crm
      )
) prospects;

-- ============================================================
-- v_crm_icp_queue — ICP-sourced LP discovery queue
-- Surfaces institutional prospects that have passed ICP scoring
-- but are not yet in CRM, with readiness labels and gate history.
-- ============================================================

CREATE OR REPLACE VIEW v_crm_icp_queue AS
SELECT
    CAST(a.allocator_id AS VARCHAR)  AS allocator_id,
    a.canonical_name                 AS investor_name,
    a.allocator_type,
    a.geography                      AS investor_location,
    i.tier                           AS icp_tier,
    i.fit_score,
    i.client_decision,
    i.client_status,
    i.core_pass,
    (
        SELECT COUNT(*) FROM relationships_effective r
        WHERE r.edge_type = 'mutual_connection'
          AND (CAST(r.source_node_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR)
            OR CAST(r.target_node_id AS VARCHAR) = CAST(a.allocator_id AS VARCHAR))
    ) AS warm_path_count,
    CASE
        WHEN i.tier = 'tier_1' THEN 'READY'
        WHEN i.tier = 'tier_2'
             AND LOWER(COALESCE(i.client_decision, '')) IN ('approved', 'approved_no_campaign')
             THEN 'READY'
        WHEN i.tier = 'tier_2'
             AND COALESCE(i.fit_score, 0) >= 0.60
             THEN 'NEAR_READY'
        ELSE 'PENDING'
    END AS readiness,
    gr.gate_verdict,
    gr.gate_session_id AS gate_session_id,
    CAST(gr.reviewed_at AS VARCHAR) AS gate_reviewed_at
FROM icp_scores i
JOIN allocators a
    ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
LEFT JOIN crm_gate_reviews gr
    ON gr.name_key = lower(regexp_replace(
        regexp_replace(
            a.canonical_name,
            '(?i)\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$',
            '',
            'g'
        ),
        '[^a-zA-Z0-9]', '', 'g'
    ))
WHERE i.icp_version = '4.1'
  AND COALESCE(i.excluded, FALSE) = FALSE
  AND COALESCE(i.core_pass, FALSE) = TRUE
  AND COALESCE(a.population, '') = 'institutional_prospect'
  AND (
      i.tier = 'tier_1'
      OR (i.tier = 'tier_2'
          AND (
              LOWER(COALESCE(i.client_decision, '')) IN ('approved', 'approved_no_campaign')
              OR COALESCE(i.fit_score, 0) >= 0.60
          )
      )
  )
  AND NOT (
      EXISTS (
          SELECT 1 FROM crm_contacts c
          WHERE lower(regexp_replace(c.investor_name, '[^a-zA-Z0-9]', '', 'g'))
              = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
      )
      OR EXISTS (
          SELECT 1 FROM crm_leads l
          WHERE l.status != 'passed'
            AND (
                l.name_key = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
                OR lower(regexp_replace(l.investor_name, '[^a-zA-Z0-9]', '', 'g'))
                    = lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g'))
            )
      )
  )
  AND lower(regexp_replace(a.canonical_name, '[^a-zA-Z0-9]', '', 'g')) NOT IN (
      SELECT name_key FROM crm_dismissed
  );

CREATE OR REPLACE VIEW v_crm_needs_enrichment AS
SELECT
    w.lead_id,
    w.investor_name,
    w.allocator_id,
    w.source,
    w.investor_type,
    w.investor_location,
    w.needs_enrichment,
    w.icp_tier,
    w.fit_score,
    'lead' AS record_type
FROM v_crm_workspace w
WHERE w.needs_enrichment
   OR w.investor_type IS NULL
   OR w.investor_location IS NULL

UNION ALL

SELECT
    NULL,
    p.investor_name,
    p.allocator_id,
    p.suggested_source,
    p.investor_type,
    p.investor_location,
    TRUE,
    p.icp_tier,
    p.fit_score,
    'prospect'
FROM v_crm_prospects p
WHERE p.investor_type IS NULL OR p.investor_location IS NULL;
