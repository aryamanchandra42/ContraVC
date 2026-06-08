-- PULSE Postgres / Supabase DDL
-- Production-portable. Mirrors duckdb.sql structure 1:1.
-- Differences: JSONB (not JSON), GIN indexes, UUID default uses gen_random_uuid().
-- Run against Supabase: psql $DATABASE_URL < schema/postgres.sql

-- Required extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- Uncomment when Phase 5-6 (vector search) is activated:
-- CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Provenance substrate
-- ============================================================

CREATE TABLE IF NOT EXISTS entities_raw (
    source_record_id    TEXT        PRIMARY KEY,
    source_file         TEXT        NOT NULL,
    source_type         TEXT        NOT NULL CHECK (source_type IN ('xlsx', 'pdf', 'docx', 'api', 'csv')),
    source_offset       TEXT        NOT NULL,
    content_hash        TEXT        NOT NULL,
    raw_content         JSONB       NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version      TEXT        NOT NULL DEFAULT '1.0'
);

CREATE INDEX IF NOT EXISTS idx_entities_raw_source_file  ON entities_raw (source_file);
CREATE INDEX IF NOT EXISTS idx_entities_raw_content_hash ON entities_raw (content_hash);

-- ============================================================
-- Canonical entities
-- ============================================================

CREATE TABLE IF NOT EXISTS allocators (
    allocator_id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name              TEXT        NOT NULL,
    aliases                     JSONB       DEFAULT '[]',
    allocator_type              TEXT,
    geography                   TEXT,
    hq_country                  TEXT,
    stage_preference            TEXT,
    check_size_min_usd          DOUBLE PRECISION,
    check_size_max_usd          DOUBLE PRECISION,
    check_size_bucket           TEXT,
    em_appetite                 TEXT,
    ai_appetite                 TEXT,
    relationship_density        DOUBLE PRECISION,
    institutional_flexibility   TEXT,
    -- Population tag: 'institutional_prospect' | 'syndicate_lp' | 'benchmark_target'.
    -- Scopes the ICP ranked list to prospects while the graph uses everyone.
    population                  TEXT,
    -- Scoring: reserved for Phase 5-6; null in Phase 1-4
    inferred_scores             JSONB,
    confidences                 JSONB,
    -- Phase 5-6 embedding placeholder (column reserved, not used yet)
    -- embedding                vector(1536),
    -- Provenance
    source_record_id            TEXT        NOT NULL,
    source_file                 TEXT        NOT NULL,
    ingested_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash                TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_allocators_canonical_name ON allocators (canonical_name);
CREATE INDEX IF NOT EXISTS idx_allocators_type           ON allocators (allocator_type);

-- Migration for pre-existing databases (no-op on fresh DBs where the column already exists).
ALTER TABLE allocators ADD COLUMN IF NOT EXISTS population TEXT;

CREATE TABLE IF NOT EXISTS funds (
    fund_id             UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name      TEXT            NOT NULL,
    aliases             JSONB           DEFAULT '[]',
    fund_type           TEXT,
    manager_name        TEXT,
    vintage_year        INTEGER,
    geography_focus     TEXT,
    strategy            TEXT,
    target_size_usd     DOUBLE PRECISION,
    close_size_usd      DOUBLE PRECISION,
    source_record_id    TEXT            NOT NULL,
    source_file         TEXT            NOT NULL,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    content_hash        TEXT            NOT NULL
);

CREATE TABLE IF NOT EXISTS interactions (
    interaction_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id            UUID        NOT NULL REFERENCES allocators (allocator_id),
    interaction_type        TEXT        NOT NULL,
    occurred_at             TIMESTAMPTZ,
    notes                   TEXT,
    sentiment               TEXT        CHECK (sentiment IN ('positive', 'neutral', 'negative', 'unknown')),
    follow_up_required      BOOLEAN     NOT NULL DEFAULT FALSE,
    follow_up_notes         TEXT,
    relationship_strength   DOUBLE PRECISION CHECK (relationship_strength BETWEEN 0 AND 1),
    progression_stage       TEXT,
    source_record_id        TEXT        NOT NULL,
    source_file             TEXT        NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash            TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interactions_allocator ON interactions (allocator_id);
CREATE INDEX IF NOT EXISTS idx_interactions_occurred  ON interactions (occurred_at);

CREATE TABLE IF NOT EXISTS investments (
    investment_id       UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    lp_id               UUID            NOT NULL REFERENCES allocators (allocator_id),
    fund_id             UUID            NOT NULL REFERENCES funds (fund_id),
    investment_date     DATE,
    commitment_usd      DOUBLE PRECISION,
    syndicate_overlap   BOOLEAN,
    co_investment_flag  BOOLEAN         NOT NULL DEFAULT FALSE,
    notes               TEXT,
    source_record_id    TEXT            NOT NULL,
    source_file         TEXT            NOT NULL,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    content_hash        TEXT            NOT NULL
);

-- ============================================================
-- Relationship graph
-- ============================================================

CREATE TABLE IF NOT EXISTS relationships (
    edge_id                 UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    source_node_id          TEXT            NOT NULL,
    source_node_type        TEXT            NOT NULL CHECK (source_node_type IN ('lp','fund','syndicate','founder','advisor','geography')),
    target_node_id          TEXT            NOT NULL,
    target_node_type        TEXT            NOT NULL CHECK (target_node_type IN ('lp','fund','syndicate','founder','advisor','geography')),
    edge_type               TEXT            NOT NULL CHECK (edge_type IN ('invested_with','introduced_by','co_invested','syndicate_overlap','mutual_connection','repeated_exposure','co_mentioned','cross_file_corroboration')),
    weight                  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    -- Temporal (populated by pulse derive)
    effective_date          DATE,
    first_seen              TIMESTAMPTZ,
    last_seen               TIMESTAMPTZ,
    last_active             TIMESTAMPTZ,
    relationship_decay_score DOUBLE PRECISION CHECK (relationship_decay_score BETWEEN 0 AND 1),
    temporal_confidence     DOUBLE PRECISION CHECK (temporal_confidence BETWEEN 0 AND 1),
    -- Uncertainty (populated by pulse derive)
    confidence              DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER         NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE PRECISION CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE PRECISION CHECK (source_agreement_score BETWEEN 0 AND 1),
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_relationships_edge_type   ON relationships (edge_type);
CREATE INDEX IF NOT EXISTS idx_relationships_source_node ON relationships (source_node_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target_node ON relationships (target_node_id);

CREATE TABLE IF NOT EXISTS relationship_evidence (
    evidence_id         UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    edge_id             UUID            NOT NULL REFERENCES relationships (edge_id),
    source_record_id    TEXT            NOT NULL REFERENCES entities_raw (source_record_id),
    evidence_type       TEXT            NOT NULL CHECK (evidence_type IN (
        'cross_file_match', 'structured_xlsx_match', 'heuristic_keyword_match',
        'llm_enriched', 'co_investment_pattern', 'graph_path_inference',
        'interaction_recurrence', 'contradicts_edge', 'contradicts_value'
    )),
    evidence_strength   DOUBLE PRECISION NOT NULL CHECK (evidence_strength BETWEEN 0 AND 1),
    confidence          DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    provenance_pointer  JSONB           NOT NULL,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_rel_evidence_edge ON relationship_evidence (edge_id);
CREATE INDEX IF NOT EXISTS idx_rel_evidence_src  ON relationship_evidence (source_record_id);

-- ============================================================
-- Signals
-- ============================================================

CREATE TABLE IF NOT EXISTS signals (
    signal_id               UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id            UUID            NOT NULL REFERENCES allocators (allocator_id),
    signal_type             TEXT            NOT NULL,
    raw_value               TEXT,
    normalized_value        DOUBLE PRECISION,
    -- Uncertainty (populated by pulse derive)
    confidence              DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER         NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE PRECISION CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE PRECISION CHECK (source_agreement_score BETWEEN 0 AND 1),
    effective_date          DATE,
    first_seen              TIMESTAMPTZ,
    last_seen               TIMESTAMPTZ,
    last_active             TIMESTAMPTZ,
    temporal_confidence     DOUBLE PRECISION CHECK (temporal_confidence BETWEEN 0 AND 1),
    source_record_id        TEXT            NOT NULL,
    source_file             TEXT            NOT NULL,
    ingested_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    content_hash            TEXT            NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_allocator ON signals (allocator_id);
CREATE INDEX IF NOT EXISTS idx_signals_type      ON signals (signal_type);

CREATE TABLE IF NOT EXISTS signal_evidence (
    evidence_id         UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id           UUID            NOT NULL REFERENCES signals (signal_id),
    source_record_id    TEXT            NOT NULL,
    evidence_type       TEXT            NOT NULL CHECK (evidence_type IN (
        'signal_heuristic', 'signal_investment_pattern', 'signal_graph_metric',
        'signal_icp_mirror', 'signal_connectivity', 'contradicts_value'
    )),
    evidence_strength   DOUBLE PRECISION NOT NULL CHECK (evidence_strength BETWEEN 0 AND 1),
    confidence          DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    provenance_pointer  JSONB           NOT NULL,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_signal_evidence_signal ON signal_evidence (signal_id);

-- ============================================================
-- Rejections
-- ============================================================

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id                UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id                UUID            NOT NULL REFERENCES allocators (allocator_id),
    rejection_type              TEXT            NOT NULL CHECK (rejection_type IN ('stated','inferred','structural')),
    reason_tags                 JSONB           DEFAULT '[]',
    stated_reason               TEXT,
    inferred_reason             TEXT,
    structural_constraint       TEXT,
    future_conversion_prob      DOUBLE PRECISION CHECK (future_conversion_prob BETWEEN 0 AND 1),
    confidence                  DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    evidence_count              INTEGER         NOT NULL DEFAULT 0,
    contradiction_score         DOUBLE PRECISION CHECK (contradiction_score BETWEEN 0 AND 1),
    source_record_id            TEXT            NOT NULL,
    source_file                 TEXT            NOT NULL,
    ingested_at                 TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    content_hash                TEXT            NOT NULL
);

-- ============================================================
-- Ontology
-- ============================================================

CREATE TABLE IF NOT EXISTS ontology_terms (
    term_id                 UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    term                    TEXT            NOT NULL,
    category                TEXT            NOT NULL CHECK (category IN ('allocator_archetype','em_signal','rejection_pattern','geography_cluster','committee_constraint')),
    description             TEXT,
    canonical_label         TEXT,
    confidence              DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
    evidence_count          INTEGER         NOT NULL DEFAULT 0,
    contradiction_score     DOUBLE PRECISION CHECK (contradiction_score BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE PRECISION CHECK (source_agreement_score BETWEEN 0 AND 1),
    first_seen              TIMESTAMPTZ,
    last_seen               TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ontology_terms_term_cat ON ontology_terms (term, category);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id                UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_id            TEXT            NOT NULL,
    entity_type             TEXT            NOT NULL CHECK (entity_type IN ('allocator','fund')),
    alias_text              TEXT            NOT NULL,
    source_file             TEXT            NOT NULL,
    confidence              DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source_agreement_score  DOUBLE PRECISION CHECK (source_agreement_score BETWEEN 0 AND 1),
    resolver_method         TEXT            NOT NULL DEFAULT 'rapidfuzz',
    ingested_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical ON entity_aliases (canonical_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_text      ON entity_aliases (alias_text);

-- ============================================================
-- Human reviews (append-only — NEVER UPDATE or DELETE)
-- ============================================================

CREATE TABLE IF NOT EXISTS human_reviews (
    review_id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type             TEXT        NOT NULL CHECK (target_type IN ('alias','allocator_archetype','ontology_term','signal','relationship_edge','rejection')),
    entity_id               TEXT        NOT NULL,
    reviewer                TEXT        NOT NULL,
    decision                TEXT        NOT NULL CHECK (decision IN ('confirm','reject','revise','defer')),
    override_payload        JSONB,
    confidence_adjustment   DOUBLE PRECISION CHECK (confidence_adjustment BETWEEN -1 AND 1),
    override_reason         TEXT,
    notes                   TEXT,
    reviewed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    supersedes              UUID        REFERENCES human_reviews (review_id)
);

CREATE INDEX IF NOT EXISTS idx_human_reviews_entity      ON human_reviews (entity_id);
CREATE INDEX IF NOT EXISTS idx_human_reviews_target_type ON human_reviews (target_type);
CREATE INDEX IF NOT EXISTS idx_human_reviews_decision    ON human_reviews (decision);

-- Enforce append-only: deny UPDATE and DELETE
-- Uncomment after initial setup when Supabase RLS is configured:
-- CREATE RULE no_update_human_reviews AS ON UPDATE TO human_reviews DO INSTEAD NOTHING;
-- CREATE RULE no_delete_human_reviews AS ON DELETE TO human_reviews DO INSTEAD NOTHING;

-- ============================================================
-- Pipeline run tracking
-- ============================================================

-- ============================================================
-- ICP Scoring
-- ============================================================

CREATE TABLE IF NOT EXISTS icp_scores (
    score_id            UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id        UUID             NOT NULL,
    icp_version         TEXT             NOT NULL DEFAULT '4.1',
    c1_asset_class_pass     BOOLEAN,
    c1_evidence             TEXT,
    c2_emerging_manager_pass BOOLEAN,
    c2_evidence             TEXT,
    c3_ai_tech_pass         BOOLEAN,
    c3_evidence             TEXT,
    c4_geography_pass       BOOLEAN,
    c4_evidence             TEXT,
    core_pass           BOOLEAN,
    excluded            BOOLEAN          NOT NULL DEFAULT FALSE,
    exclusion_reason    TEXT,
    s1_ai_signal        DOUBLE PRECISION,
    s2_emerging_manager DOUBLE PRECISION,
    s3_lp_type          DOUBLE PRECISION,
    s4_decision_speed   DOUBLE PRECISION,
    s5_stage            DOUBLE PRECISION,
    s6_clean_profile    DOUBLE PRECISION,
    s7_proxy_fund       DOUBLE PRECISION,
    fit_score           DOUBLE PRECISION CHECK (fit_score BETWEEN 0 AND 1),
    tier                TEXT             CHECK (tier IN ('tier_1','tier_2','tier_3','tier_4')),
    client_status       TEXT,
    client_decision     TEXT             CHECK (client_decision IN ('approved','approved_no_campaign','rejected','pending')),
    stated_reason       TEXT,
    data_miner_comment  TEXT,
    source_sheet        TEXT,
    source_row          INTEGER,
    source_file         TEXT,
    scored_at           TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_icp_scores_allocator ON icp_scores (allocator_id);

-- ============================================================
-- External benchmark rankings (e.g. ContraVC Top 200)
-- Independent pre-computed LP ranking used to calibrate the ICP scorer.
-- NOT authored by pulse derive.
-- ============================================================

CREATE TABLE IF NOT EXISTS benchmark_rankings (
    benchmark_id        UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    allocator_id        UUID,
    external_name       TEXT             NOT NULL,
    ranking_source      TEXT             NOT NULL,
    rank                INTEGER,
    priority_score      DOUBLE PRECISION,
    tier                TEXT,
    prior_fund_lp       BOOLEAN,
    spvs_backed         INTEGER,
    funds_backed        INTEGER,
    median_check_usd    DOUBLE PRECISION,
    total_invested_usd  DOUBLE PRECISION,
    al_activity_usd     DOUBLE PRECISION,
    linkedin_url        TEXT,
    source_record_id    TEXT             NOT NULL,
    source_file         TEXT             NOT NULL,
    content_hash        TEXT             NOT NULL,
    ingested_at         TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_benchmark_rankings_allocator ON benchmark_rankings (allocator_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_rankings_source    ON benchmark_rankings (ranking_source);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    stage                   TEXT        NOT NULL CHECK (stage IN ('ingest','normalize','extract','derive','graph','review','score','calibrate','research')),
    status                  TEXT        NOT NULL CHECK (status IN ('running','completed','failed')),
    params                  JSONB       DEFAULT '{}',
    artifact_uris           JSONB       DEFAULT '[]',
    derivation_params_hash  TEXT,
    started_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ,
    error                   TEXT,
    rows_processed          INTEGER     NOT NULL DEFAULT 0,
    rows_written            INTEGER     NOT NULL DEFAULT 0
);
