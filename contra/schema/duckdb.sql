-- PULSE DuckDB DDL
-- Local iteration database. Mirrors postgres.sql structure 1:1.
-- Key differences vs Postgres: JSON (not JSONB), no GIN indexes, TIMESTAMPTZ as TIMESTAMP WITH TIME ZONE.
-- Run: duckdb pulse.duckdb < schema/duckdb.sql

-- ============================================================
-- Provenance substrate
-- ============================================================

CREATE TABLE IF NOT EXISTS entities_raw (
    source_record_id    VARCHAR PRIMARY KEY,
    source_file         VARCHAR NOT NULL,
    source_type         VARCHAR NOT NULL CHECK (source_type IN ('xlsx', 'pdf', 'docx', 'api', 'csv')),
    source_offset       VARCHAR NOT NULL,
    content_hash        VARCHAR NOT NULL,
    raw_content         JSON    NOT NULL,
    ingested_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    schema_version      VARCHAR NOT NULL DEFAULT '1.0'
);

-- ============================================================
-- Canonical entities
-- ============================================================

CREATE TABLE IF NOT EXISTS allocators (
    allocator_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name              VARCHAR NOT NULL,
    aliases                     JSON    DEFAULT '[]',
    allocator_type              VARCHAR,
    geography                   VARCHAR,
    hq_country                  VARCHAR,
    stage_preference            VARCHAR,
    check_size_min_usd          DOUBLE,
    check_size_max_usd          DOUBLE,
    check_size_bucket           VARCHAR,
    em_appetite                 VARCHAR,
    ai_appetite                 VARCHAR,
    relationship_density        DOUBLE,
    institutional_flexibility   VARCHAR,
    -- Population tag: which data universe this LP belongs to.
    -- 'institutional_prospect' (ICP scoping list) | 'syndicate_lp' (AngelList co-invest roster) | 'benchmark_target'.
    -- Used to scope the ICP ranked list to prospects while the graph uses everyone.
    population                  VARCHAR,
    -- Scoring: reserved for Phase 5-6; null in Phase 1-4
    inferred_scores             JSON,
    confidences                 JSON,
    -- Provenance
    source_record_id            VARCHAR NOT NULL,
    source_file                 VARCHAR NOT NULL,
    ingested_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash                VARCHAR NOT NULL,
    created_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Migration for pre-existing databases (no-op on fresh DBs where the column already exists).
ALTER TABLE allocators ADD COLUMN IF NOT EXISTS population VARCHAR;

CREATE TABLE IF NOT EXISTS funds (
    fund_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      VARCHAR NOT NULL,
    aliases             JSON    DEFAULT '[]',
    fund_type           VARCHAR,
    manager_name        VARCHAR,
    vintage_year        INTEGER,
    geography_focus     VARCHAR,
    strategy            VARCHAR,
    target_size_usd     DOUBLE,
    close_size_usd      DOUBLE,
    source_record_id    VARCHAR NOT NULL,
    source_file         VARCHAR NOT NULL,
    ingested_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    interaction_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id            UUID NOT NULL,
    interaction_type        VARCHAR NOT NULL,
    occurred_at             TIMESTAMP WITH TIME ZONE,
    notes                   VARCHAR,
    sentiment               VARCHAR CHECK (sentiment IN ('positive', 'neutral', 'negative', 'unknown')),
    follow_up_required      BOOLEAN NOT NULL DEFAULT FALSE,
    follow_up_notes         VARCHAR,
    relationship_strength   DOUBLE CHECK (relationship_strength BETWEEN 0 AND 1),
    progression_stage       VARCHAR,
    source_record_id        VARCHAR NOT NULL,
    source_file             VARCHAR NOT NULL,
    ingested_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS investments (
    investment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lp_id               UUID NOT NULL,
    fund_id             UUID NOT NULL,
    investment_date     DATE,
    commitment_usd      DOUBLE,
    syndicate_overlap   BOOLEAN,
    co_investment_flag  BOOLEAN NOT NULL DEFAULT FALSE,
    notes               VARCHAR,
    source_record_id    VARCHAR NOT NULL,
    source_file         VARCHAR NOT NULL,
    ingested_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash        VARCHAR NOT NULL
);

-- ============================================================
-- Relationship graph
-- ============================================================

CREATE TABLE IF NOT EXISTS relationships (
    edge_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_node_id          VARCHAR NOT NULL,
    source_node_type        VARCHAR NOT NULL CHECK (source_node_type IN ('lp','fund','syndicate','founder','advisor','geography')),
    target_node_id          VARCHAR NOT NULL,
    target_node_type        VARCHAR NOT NULL CHECK (target_node_type IN ('lp','fund','syndicate','founder','advisor','geography')),
    edge_type               VARCHAR NOT NULL CHECK (edge_type IN ('invested_with','introduced_by','co_invested','syndicate_overlap','mutual_connection','repeated_exposure','co_mentioned','cross_file_corroboration')),
    weight                  DOUBLE NOT NULL DEFAULT 1.0,
    -- Temporal (populated by pulse derive)
    effective_date          DATE,
    first_seen              TIMESTAMP WITH TIME ZONE,
    last_seen               TIMESTAMP WITH TIME ZONE,
    last_active             TIMESTAMP WITH TIME ZONE,
    relationship_decay_score DOUBLE CHECK (relationship_decay_score BETWEEN 0 AND 1),
    temporal_confidence     DOUBLE CHECK (temporal_confidence BETWEEN 0 AND 1),
    -- Uncertainty (populated by pulse derive)
    confidence              DOUBLE CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE CHECK (source_agreement_score BETWEEN 0 AND 1),
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS relationship_evidence (
    evidence_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id             UUID NOT NULL,  -- → relationships.edge_id
    source_record_id    VARCHAR NOT NULL,  -- → entities_raw.source_record_id
    evidence_type       VARCHAR NOT NULL CHECK (evidence_type IN (
        'cross_file_match', 'structured_xlsx_match', 'heuristic_keyword_match',
        'llm_enriched', 'co_investment_pattern', 'graph_path_inference',
        'interaction_recurrence', 'contradicts_edge', 'contradicts_value'
    )),
    evidence_strength   DOUBLE NOT NULL CHECK (evidence_strength BETWEEN 0 AND 1),
    confidence          DOUBLE NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    timestamp           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    provenance_pointer  JSON NOT NULL,  -- {source_file, source_offset, row_id}
    notes               VARCHAR
);

-- ============================================================
-- Signals
-- ============================================================

CREATE TABLE IF NOT EXISTS signals (
    signal_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id            UUID NOT NULL,
    signal_type             VARCHAR NOT NULL,
    raw_value               VARCHAR,
    normalized_value        DOUBLE,
    -- Uncertainty (populated by pulse derive)
    confidence              DOUBLE CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE CHECK (source_agreement_score BETWEEN 0 AND 1),
    effective_date          DATE,
    first_seen              TIMESTAMP WITH TIME ZONE,
    last_seen               TIMESTAMP WITH TIME ZONE,
    last_active             TIMESTAMP WITH TIME ZONE,
    temporal_confidence     DOUBLE CHECK (temporal_confidence BETWEEN 0 AND 1),
    source_record_id        VARCHAR NOT NULL,
    source_file             VARCHAR NOT NULL,
    ingested_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_evidence (
    evidence_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id           UUID NOT NULL,
    source_record_id    VARCHAR NOT NULL,
    evidence_type       VARCHAR NOT NULL CHECK (evidence_type IN (
        'signal_heuristic', 'signal_investment_pattern', 'signal_graph_metric',
        'signal_icp_mirror', 'signal_connectivity', 'contradicts_value'
    )),
    evidence_strength   DOUBLE NOT NULL CHECK (evidence_strength BETWEEN 0 AND 1),
    confidence          DOUBLE NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    timestamp           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    provenance_pointer  JSON NOT NULL,
    notes               VARCHAR
);

-- ============================================================
-- Rejections
-- ============================================================

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id                UUID NOT NULL,
    rejection_type              VARCHAR NOT NULL CHECK (rejection_type IN ('stated','inferred','structural')),
    reason_tags                 JSON DEFAULT '[]',
    stated_reason               VARCHAR,
    inferred_reason             VARCHAR,
    structural_constraint       VARCHAR,
    future_conversion_prob      DOUBLE CHECK (future_conversion_prob BETWEEN 0 AND 1),
    confidence                  DOUBLE CHECK (confidence BETWEEN 0 AND 1),
    evidence_count              INTEGER NOT NULL DEFAULT 0,
    contradiction_score         DOUBLE CHECK (contradiction_score BETWEEN 0 AND 1),
    source_record_id            VARCHAR NOT NULL,
    source_file                 VARCHAR NOT NULL,
    ingested_at                 TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    content_hash                VARCHAR NOT NULL
);

-- ============================================================
-- Ontology
-- ============================================================

CREATE TABLE IF NOT EXISTS ontology_terms (
    term_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term                    VARCHAR NOT NULL,
    category                VARCHAR NOT NULL CHECK (category IN ('allocator_archetype','em_signal','rejection_pattern','geography_cluster','committee_constraint')),
    description             VARCHAR,
    canonical_label         VARCHAR,
    confidence              DOUBLE CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE CHECK (source_agreement_score BETWEEN 0 AND 1),
    first_seen              TIMESTAMP WITH TIME ZONE,
    last_seen               TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id            VARCHAR NOT NULL,
    entity_type             VARCHAR NOT NULL CHECK (entity_type IN ('allocator','fund')),
    alias_text              VARCHAR NOT NULL,
    source_file             VARCHAR NOT NULL,
    confidence              DOUBLE NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE CHECK (source_agreement_score BETWEEN 0 AND 1),
    resolver_method         VARCHAR NOT NULL DEFAULT 'rapidfuzz',
    ingested_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Human reviews (append-only)
-- ============================================================

CREATE TABLE IF NOT EXISTS human_reviews (
    review_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type             VARCHAR NOT NULL CHECK (target_type IN ('alias','allocator_archetype','ontology_term','signal','relationship_edge','rejection')),
    entity_id               VARCHAR NOT NULL,
    reviewer                VARCHAR NOT NULL,
    decision                VARCHAR NOT NULL CHECK (decision IN ('confirm','reject','revise','defer')),
    override_payload        JSON,
    confidence_adjustment   DOUBLE CHECK (confidence_adjustment BETWEEN -1 AND 1),
    override_reason         VARCHAR,
    notes                   VARCHAR,
    reviewed_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    supersedes              UUID  -- → human_reviews.review_id
);

-- ============================================================
-- Pipeline run tracking
-- ============================================================

-- ============================================================
-- ICP Scoring
-- ============================================================

CREATE TABLE IF NOT EXISTS icp_scores (
    score_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id        UUID NOT NULL,
    icp_version         VARCHAR NOT NULL DEFAULT '4.1',
    -- Core criteria C1–C4 (must all pass) — ICP v4.1 semantics
    c1_asset_class_pass     BOOLEAN,   -- C1: VC fund LP commitment
    c1_evidence             VARCHAR,
    c2_emerging_manager_pass BOOLEAN,  -- C2: emerging manager appetite
    c2_evidence             VARCHAR,
    c3_ai_tech_pass         BOOLEAN,   -- C3: AI / tech thesis
    c3_evidence             VARCHAR,
    c4_geography_pass       BOOLEAN,   -- C4: Asia / NA / ME geographic fit
    c4_evidence             VARCHAR,
    core_pass           BOOLEAN,
    -- Exclusions
    excluded            BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason    VARCHAR,
    -- Soft signals S1–S7 (0.0 – 1.0)
    s1_ai_signal        DOUBLE,
    s2_emerging_manager DOUBLE,
    s3_lp_type          DOUBLE,
    s4_decision_speed   DOUBLE,
    s5_stage            DOUBLE,
    s6_clean_profile    DOUBLE,
    s7_proxy_fund       DOUBLE,
    -- Aggregate
    fit_score           DOUBLE CHECK (fit_score BETWEEN 0 AND 1),
    tier                VARCHAR CHECK (tier IN ('tier_1','tier_2','tier_3','tier_4')),
    -- Human decision from source file
    client_status       VARCHAR,
    client_decision     VARCHAR CHECK (client_decision IN ('approved','approved_no_campaign','rejected','pending')),
    stated_reason       VARCHAR,
    data_miner_comment  VARCHAR,
    -- Source provenance
    source_sheet        VARCHAR,
    source_row          INTEGER,
    source_file         VARCHAR,
    scored_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_icp_scores_allocator ON icp_scores (allocator_id);

-- ============================================================
-- External benchmark rankings (e.g. ContraVC Top 200)
-- An independent, pre-computed LP ranking used to calibrate / validate
-- PULSE's own ICP scorer. NOT authored by pulse derive.
-- ============================================================

CREATE TABLE IF NOT EXISTS benchmark_rankings (
    benchmark_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id        UUID,                       -- resolved PULSE allocator (null if unmatched)
    external_name       VARCHAR NOT NULL,
    ranking_source      VARCHAR NOT NULL,           -- e.g. 'contravc_top200'
    rank                INTEGER,
    priority_score      DOUBLE,
    tier                VARCHAR,
    prior_fund_lp       BOOLEAN,
    spvs_backed         INTEGER,
    funds_backed        INTEGER,
    median_check_usd    DOUBLE,
    total_invested_usd  DOUBLE,
    al_activity_usd     DOUBLE,
    linkedin_url        VARCHAR,
    source_record_id    VARCHAR NOT NULL,
    source_file         VARCHAR NOT NULL,
    content_hash        VARCHAR NOT NULL,
    ingested_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_benchmark_rankings_allocator ON benchmark_rankings (allocator_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_rankings_source    ON benchmark_rankings (ranking_source);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stage                   VARCHAR NOT NULL CHECK (stage IN ('ingest','normalize','extract','derive','graph','review','score','calibrate','research')),
    status                  VARCHAR NOT NULL CHECK (status IN ('running','completed','failed')),
    params                  JSON DEFAULT '{}',
    artifact_uris           JSON DEFAULT '[]',
    derivation_params_hash  VARCHAR,
    started_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMP WITH TIME ZONE,
    error                   VARCHAR,
    rows_processed          INTEGER NOT NULL DEFAULT 0,
    rows_written            INTEGER NOT NULL DEFAULT 0
);

-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_entities_raw_source_file     ON entities_raw (source_file);
CREATE INDEX IF NOT EXISTS idx_entities_raw_content_hash    ON entities_raw (content_hash);
CREATE INDEX IF NOT EXISTS idx_relationship_evidence_edge   ON relationship_evidence (edge_id);
CREATE INDEX IF NOT EXISTS idx_relationship_evidence_src    ON relationship_evidence (source_record_id);
CREATE INDEX IF NOT EXISTS idx_relationships_edge_type      ON relationships (edge_type);
CREATE INDEX IF NOT EXISTS idx_relationships_source_node    ON relationships (source_node_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target_node    ON relationships (target_node_id);
CREATE INDEX IF NOT EXISTS idx_signals_allocator            ON signals (allocator_id);
CREATE INDEX IF NOT EXISTS idx_human_reviews_entity         ON human_reviews (entity_id);
CREATE INDEX IF NOT EXISTS idx_human_reviews_target_type    ON human_reviews (target_type);
CREATE INDEX IF NOT EXISTS idx_allocators_canonical_name    ON allocators (canonical_name);

-- ============================================================
-- Contra extension tables (CRM, ICP rules, data catalog)
-- ============================================================

CREATE TABLE IF NOT EXISTS crm_contacts (
    contact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_name       VARCHAR NOT NULL,
    name_key            VARCHAR NOT NULL,
    investor_type       VARCHAR,
    investor_location   VARCHAR,
    investor_details    VARCHAR,
    contacts_json       JSON,
    crm_status          VARCHAR,
    source_file         VARCHAR NOT NULL DEFAULT 'export.csv',
    ingested_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_contacts_name_key ON crm_contacts (name_key);

CREATE TABLE IF NOT EXISTS crm_leads (
    lead_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    investor_name       VARCHAR NOT NULL,
    name_key            VARCHAR NOT NULL,
    allocator_id        VARCHAR,
    source              VARCHAR NOT NULL,
    status              VARCHAR NOT NULL DEFAULT 'active',
    investor_type       VARCHAR,
    investor_location   VARCHAR,
    investor_details    VARCHAR,
    contacts_json       JSON,
    pipeline_stage      VARCHAR,
    computed_score      DOUBLE,
    manual_rank         INTEGER,
    gate_session_id     VARCHAR,
    gate_verdict        VARCHAR,
    gate_confidence     VARCHAR,
    gate_summary        VARCHAR,
    gate_reasons_json   JSON,
    appetite_json       JSON,
    icp_tier            VARCHAR,
    fit_score           DOUBLE,
    contra_rank         INTEGER,
    warm_path_count     INTEGER,
    syndicate_score     DOUBLE,
    needs_enrichment    BOOLEAN NOT NULL DEFAULT FALSE,
    source_file         VARCHAR,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_leads_name_key ON crm_leads (name_key);
CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads (status);
CREATE INDEX IF NOT EXISTS idx_crm_leads_computed_score ON crm_leads (computed_score);

CREATE TABLE IF NOT EXISTS icp_rules (
    rule_id             VARCHAR PRIMARY KEY,
    category            VARCHAR NOT NULL,
    rule_name           VARCHAR NOT NULL,
    rule_text           VARCHAR NOT NULL,
    weight              DOUBLE,
    source_sheet        VARCHAR NOT NULL,
    source_file         VARCHAR NOT NULL DEFAULT 'MyAsiaVC LP Scoping.xlsx'
);

CREATE TABLE IF NOT EXISTS data_catalog (
    catalog_key         VARCHAR PRIMARY KEY,
    description         VARCHAR NOT NULL,
    row_count           INTEGER,
    source_files        JSON,
    last_refreshed      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
