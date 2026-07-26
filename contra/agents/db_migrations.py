"""
One-shot schema migrations for existing pulse.duckdb databases.

CREATE TABLE IF NOT EXISTS does not alter existing tables; migrations here
rebuild icp_scores with v4.1 column names when legacy v4.0 columns are detected.
"""

from __future__ import annotations

from typing import Set


def _icp_columns(con) -> Set[str]:
    try:
        rows = con.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'icp_scores'
            """
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def migrate_icp_scores_v41(con) -> bool:
    """
    Rebuild icp_scores with ICP v4.1 column names, remapping legacy column data.
    Returns True if migration ran.
    """
    cols = _icp_columns(con)
    if not cols:
        return False

    if "s1_ai_signal" in cols:
        return False

    if "s1_lp_type_match" not in cols:
        return False

    con.execute("DROP VIEW IF EXISTS calibration_overlay")

    con.execute(
        """
        CREATE TABLE icp_scores_v41 (
            score_id            UUID PRIMARY KEY,
            allocator_id        UUID NOT NULL,
            icp_version         VARCHAR NOT NULL DEFAULT '4.1',
            c1_asset_class_pass     BOOLEAN,
            c1_evidence             VARCHAR,
            c2_emerging_manager_pass BOOLEAN,
            c2_evidence             VARCHAR,
            c3_ai_tech_pass         BOOLEAN,
            c3_evidence             VARCHAR,
            c4_geography_pass       BOOLEAN,
            c4_evidence             VARCHAR,
            core_pass           BOOLEAN,
            excluded            BOOLEAN NOT NULL DEFAULT FALSE,
            exclusion_reason    VARCHAR,
            s1_ai_signal        DOUBLE,
            s2_emerging_manager DOUBLE,
            s3_lp_type          DOUBLE,
            s4_decision_speed   DOUBLE,
            s5_stage            DOUBLE,
            s6_clean_profile    DOUBLE,
            s7_proxy_fund       DOUBLE,
            fit_score           DOUBLE,
            tier                VARCHAR,
            client_status       VARCHAR,
            client_decision     VARCHAR,
            stated_reason       VARCHAR,
            data_miner_comment  VARCHAR,
            source_sheet        VARCHAR,
            source_row          INTEGER,
            source_file         VARCHAR,
            scored_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )

    con.execute(
        """
        INSERT INTO icp_scores_v41 (
            score_id, allocator_id, icp_version,
            c1_asset_class_pass, c1_evidence,
            c2_emerging_manager_pass, c2_evidence,
            c3_ai_tech_pass, c3_evidence,
            c4_geography_pass, c4_evidence,
            core_pass, excluded, exclusion_reason,
            s1_ai_signal, s2_emerging_manager, s3_lp_type,
            s4_decision_speed, s5_stage, s6_clean_profile, s7_proxy_fund,
            fit_score, tier, client_status, client_decision,
            stated_reason, data_miner_comment,
            source_sheet, source_row, source_file, scored_at
        )
        SELECT
            score_id, allocator_id, icp_version,
            c1_asset_class_pass, c1_evidence,
            c2_sector_pass, c2_evidence,
            c3_region_pass, c3_evidence,
            NULL, NULL,
            core_pass, excluded, exclusion_reason,
            s1_lp_type_match, s2_geography_match, s3_ai_explicit,
            s4_stage_match, s5_no_conflict_flag, NULL, NULL,
            fit_score, tier, client_status, client_decision,
            stated_reason, data_miner_comment,
            source_sheet, source_row, source_file, scored_at
        FROM icp_scores
        """
    )

    con.execute("DROP TABLE icp_scores")
    con.execute("ALTER TABLE icp_scores_v41 RENAME TO icp_scores")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_icp_scores_allocator ON icp_scores (allocator_id)"
    )
    return True


def migrate_pipeline_runs_stage_check(con) -> bool:
    """
    Expand the pipeline_runs.stage CHECK constraint to include 'calibrate' and 'research'.

    DuckDB does not support ALTER TABLE ... ALTER COLUMN ... SET CHECK, so we:
      1. Rename the old table to a backup.
      2. Create a new table with the expanded CHECK.
      3. Copy all existing rows across.
      4. Drop the backup.

    Safe to run multiple times (no-op if 'research' already accepted).
    Returns True if migration ran.
    """
    # Probe: try inserting a sentinel research row — if it succeeds, no migration needed.
    try:
        con.execute(
            """
            INSERT INTO pipeline_runs (run_id, stage, status, started_at)
            VALUES (gen_random_uuid(), 'research', 'running', NOW())
            """
        )
        # Delete the probe row immediately
        con.execute(
            "DELETE FROM pipeline_runs WHERE stage = 'research' AND error IS NULL "
            "AND rows_processed = 0 AND rows_written = 0 AND completed_at IS NULL"
        )
        return False  # constraint already allows 'research' — nothing to do
    except Exception:
        pass  # constraint is still narrow — proceed with migration

    con.execute("ALTER TABLE pipeline_runs RENAME TO pipeline_runs_old")
    con.execute(
        """
        CREATE TABLE pipeline_runs (
            run_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            stage                   VARCHAR NOT NULL CHECK (stage IN (
                'ingest','normalize','extract','derive','graph',
                'review','score','calibrate','research'
            )),
            status                  VARCHAR NOT NULL CHECK (status IN ('running','completed','failed')),
            params                  JSON DEFAULT '{}',
            artifact_uris           JSON DEFAULT '[]',
            derivation_params_hash  VARCHAR,
            started_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            completed_at            TIMESTAMP WITH TIME ZONE,
            error                   VARCHAR,
            rows_processed          INTEGER NOT NULL DEFAULT 0,
            rows_written            INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute(
        """
        INSERT INTO pipeline_runs
        SELECT * FROM pipeline_runs_old
        """
    )
    con.execute("DROP TABLE pipeline_runs_old")
    return True


def _table_exists(con, name: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = ?
            """,
            [name],
        ).fetchone()
        return row is not None
    except Exception:
        return False


def migrate_signal_expansion(con) -> bool:
    """Add signal_evidence table and relax signals.signal_type CHECK constraint."""
    if not _table_exists(con, "signals"):
        return False

    ran = False

    if not _table_exists(con, "signal_evidence"):
        con.execute(
            """
            CREATE TABLE signal_evidence (
                evidence_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                signal_id           UUID NOT NULL,
                source_record_id    VARCHAR NOT NULL,
                evidence_type       VARCHAR NOT NULL,
                evidence_strength   DOUBLE NOT NULL,
                confidence          DOUBLE NOT NULL,
                timestamp           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                provenance_pointer  JSON NOT NULL,
                notes               VARCHAR
            )
            """
        )
        ran = True

    # Detect legacy CHECK by probing bridge_strength insert on a scratch row
    try:
        con.execute(
            """
            INSERT INTO signals (
                signal_id, allocator_id, signal_type, normalized_value,
                evidence_count, source_record_id, source_file, content_hash
            )
            SELECT
                gen_random_uuid(),
                (SELECT allocator_id FROM allocators LIMIT 1),
                'bridge_strength', 0.0, 0, 'migration_probe', 'migration', 'probe'
            """
        )
        con.execute(
            "DELETE FROM signals WHERE content_hash = 'probe' AND source_file = 'migration'"
        )
    except Exception:
        # Legacy CHECK on signal_type — rebuild signals table, preserve signal_evidence.
        con.execute("CREATE TABLE signals_expanded AS SELECT * FROM signals")
        con.execute("DROP TABLE signals")
        con.execute("ALTER TABLE signals_expanded RENAME TO signals")
        if not _table_exists(con, "signal_evidence"):
            con.execute(
                """
                CREATE TABLE signal_evidence (
                    evidence_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    signal_id           UUID NOT NULL,
                    source_record_id    VARCHAR NOT NULL,
                    evidence_type       VARCHAR NOT NULL,
                    evidence_strength   DOUBLE NOT NULL,
                    confidence          DOUBLE NOT NULL,
                    timestamp           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    provenance_pointer  JSON NOT NULL,
                    notes               VARCHAR
                )
                """
            )
        ran = True

    return ran


def migrate_contra_extension(con) -> bool:
    """Add Contra-specific tables: crm_contacts, icp_rules, data_catalog."""
    ran = False
    if not _table_exists(con, "crm_contacts"):
        con.execute(
            """
            CREATE TABLE crm_contacts (
                contact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                investor_name       VARCHAR NOT NULL,
                name_key            VARCHAR NOT NULL,
                investor_type       VARCHAR,
                investor_location   VARCHAR,
                investor_details    VARCHAR,
                contacts_json       JSON,
                crm_status          VARCHAR,
                source_file         VARCHAR NOT NULL DEFAULT 'export.csv',
                ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_contacts_name_key ON crm_contacts(name_key)"
        )
        ran = True
    if not _table_exists(con, "icp_rules"):
        con.execute(
            """
            CREATE TABLE icp_rules (
                rule_id             VARCHAR PRIMARY KEY,
                category            VARCHAR NOT NULL,
                rule_name           VARCHAR NOT NULL,
                rule_text           VARCHAR NOT NULL,
                weight              DOUBLE,
                source_sheet        VARCHAR NOT NULL,
                source_file         VARCHAR NOT NULL DEFAULT 'MyAsiaVC LP Scoping.xlsx'
            )
            """
        )
        ran = True
    if not _table_exists(con, "data_catalog"):
        con.execute(
            """
            CREATE TABLE data_catalog (
                catalog_key         VARCHAR PRIMARY KEY,
                description         VARCHAR NOT NULL,
                row_count           INTEGER,
                source_files        JSON,
                last_refreshed      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        ran = True
    if not _table_exists(con, "allocator_contacts"):
        con.execute(
            """
            CREATE TABLE allocator_contacts (
                contact_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                allocator_id        VARCHAR NOT NULL,
                source              VARCHAR NOT NULL,
                full_name           VARCHAR,
                email               VARCHAR,
                linkedin_url        VARCHAR,
                title               VARCHAR,
                company             VARCHAR,
                location            VARCHAR,
                match_confidence    DOUBLE,
                ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_allocator_contacts_alloc ON allocator_contacts(allocator_id)"
        )
        ran = True
    return ran


def migrate_crm_leads(con) -> bool:
    """Add operational crm_leads table for gate writes and ranked CRM workspace."""
    if _table_exists(con, "crm_leads"):
        return False
    con.execute(
        """
        CREATE TABLE crm_leads (
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
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_name_key ON crm_leads(name_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads(status)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_leads_computed_score ON crm_leads(computed_score)"
    )
    return True


def migrate_crm_dismissed(con) -> bool:
    """
    Add crm_dismissed table — tracks names removed from upgrade/prospect queues.

    Records are written when a user dismisses a prospect or upgrade candidate.
    The prospects and enrichment API endpoints filter these names out so they
    never resurface. Dismissed leads from crm_leads are soft-deleted here too.
    """
    if _table_exists(con, "crm_dismissed"):
        return False
    con.execute(
        """
        CREATE TABLE crm_dismissed (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            investor_name   VARCHAR NOT NULL,
            name_key        VARCHAR NOT NULL,
            reason          VARCHAR NOT NULL DEFAULT 'dismissed',
            note            VARCHAR,
            dismissed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_dismissed_name_key ON crm_dismissed(name_key)"
    )
    return True


def migrate_crm_gate_reviews(con) -> bool:
    """
    Add crm_gate_reviews table — tracks every Gate screening result for an LP
    name, regardless of whether the LP was added to CRM.

    Upserted on each POST /api/gate call so the ICP queue can show
    Needs Gate / Gated: YES|REVIEW|NO without requiring the LP to be in crm_leads.
    One row per name_key (latest verdict wins).
    """
    if _table_exists(con, "crm_gate_reviews"):
        return False
    con.execute(
        """
        CREATE TABLE crm_gate_reviews (
            name_key        VARCHAR PRIMARY KEY,
            investor_name   VARCHAR NOT NULL,
            allocator_id    VARCHAR,
            gate_verdict    VARCHAR NOT NULL,
            gate_session_id VARCHAR,
            reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_gate_reviews_verdict ON crm_gate_reviews(gate_verdict)"
    )
    return True


def migrate_lp_dossiers(con) -> bool:
    """
    Add lp_dossiers table — durable institutional memory for screened LPs.

    One row per name_key. Every YES/REVIEW gate run upserts here so the full
    research (commitments, appetite, sources, web notes, verdict history) outlives
    the 30-minute in-memory gate session. Outreach events append to
    outreach_history so the dossier becomes the single place to look up
    everything known about a confirmed LP.
    """
    if _table_exists(con, "lp_dossiers"):
        return False
    con.execute(
        """
        CREATE TABLE lp_dossiers (
            name_key            VARCHAR PRIMARY KEY,
            investor_name       VARCHAR NOT NULL,
            allocator_id        VARCHAR,
            latest_verdict      VARCHAR,
            latest_session_id   VARCHAR,
            verdict_model       VARCHAR,
            lp_commitments_json JSON,
            appetite_json       JSON,
            sources_json        JSON,
            research_notes      VARCHAR,
            verdict_history_json JSON,
            outreach_history_json JSON,
            analyst_notes       VARCHAR,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    return True


def migrate_crm_outreach(con) -> bool:
    """
    Add crm_outreach_drafts table — personalized outreach emails per CRM lead.

    Drafts are generated by the outreach personalization agent from the lead's
    dossier (gate intel, commitments, warm paths) and tracked through
    draft → approved → sent.
    """
    if _table_exists(con, "crm_outreach_drafts"):
        return False
    con.execute(
        """
        CREATE TABLE crm_outreach_drafts (
            draft_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            lead_id         VARCHAR NOT NULL,
            investor_name   VARCHAR NOT NULL,
            subject         VARCHAR NOT NULL,
            body            VARCHAR NOT NULL,
            tone            VARCHAR,
            model           VARCHAR,
            personalization_json JSON,
            status          VARCHAR NOT NULL DEFAULT 'draft',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_crm_outreach_lead ON crm_outreach_drafts(lead_id)"
    )
    return True


def migrate_crm_rejection_tracking(con) -> bool:
    """
    Add rejection tracking to crm_leads and lp_dossiers.

    crm_leads:
      - rejection_reason VARCHAR — structured code from confirmed outreach rejections:
          fund_size        LP's minimum fund size > $30M (e.g. Next Legacy Partners)
          geo_mandate      Explicit US/Europe-only mandate (e.g. Moses Capital)
          deployment_pause LP paused new commitments; set revisit_date and suppress
          placement_agent  LP proposed placement-agent arrangement; escalate to GP
          other            Any other reason; see rejection_note for details
      - rejection_note  VARCHAR — free-text detail / LP's exact words
      - revisit_date    DATE    — when to re-engage (used for deployment_pause)

    lp_dossiers:
      - rejection_reason VARCHAR — mirrors crm_leads for dossier-level tracking
      - revisit_date    DATE

    Safe to re-run: uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    Returns True if any column was added.
    """
    ran = False

    # crm_leads columns
    leads_existing = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'crm_leads'"
        ).fetchall()
    }
    for col, ddl in [
        ("rejection_reason", "VARCHAR"),
        ("rejection_note",   "VARCHAR"),
        ("revisit_date",     "DATE"),
    ]:
        if col not in leads_existing:
            con.execute(f"ALTER TABLE crm_leads ADD COLUMN IF NOT EXISTS {col} {ddl}")
            ran = True

    # lp_dossiers columns
    dossier_existing = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'lp_dossiers'"
        ).fetchall()
    }
    for col, ddl in [
        ("rejection_reason", "VARCHAR"),
        ("revisit_date",     "DATE"),
    ]:
        if col not in dossier_existing:
            con.execute(f"ALTER TABLE lp_dossiers ADD COLUMN IF NOT EXISTS {col} {ddl}")
            ran = True

    return ran


def migrate_lead_scorecards(con) -> bool:
    """
    Add lead_scorecards — the visible 5-check evaluation per LP name.

    One row per name_key (latest wins). Each row stores the verdict
    (qualified/review/rejected), a plain-language reason, the yes-reason hook,
    and checks_json: five checks each with status + evidence quote + source URL.
    Written by contra.scorecard on gate runs and prospector runs.
    """
    if _table_exists(con, "lead_scorecards"):
        return False
    con.execute(
        """
        CREATE TABLE lead_scorecards (
            name_key        VARCHAR PRIMARY KEY,
            investor_name   VARCHAR NOT NULL,
            verdict         VARCHAR NOT NULL,
            verdict_reason  VARCHAR,
            yes_reason      VARCHAR,
            yes_evidence    VARCHAR,
            checks_json     JSON NOT NULL,
            source          VARCHAR NOT NULL DEFAULT 'gate',
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_scorecards_verdict ON lead_scorecards(verdict)"
    )
    return True


def migrate_prospector(con) -> bool:
    """
    Add the Prospector agent tables:

    prospector_seeds      — what the mining agent searches from (peer funds,
                            confirmed LPs, query templates). Seeds rotate by
                            last_mined_at so runs don't repeat themselves.
    prospector_candidates — every candidate ever surfaced, with its discovery
                            evidence and lifecycle status. Nothing silently
                            disappears; dedupe checks this table too.
    prospector_runs       — one row per mining run: budget, funnel counts,
                            errors. This is the agent's audit log.
    """
    ran = False
    if not _table_exists(con, "prospector_seeds"):
        con.execute(
            """
            CREATE TABLE prospector_seeds (
                seed_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                seed_type       VARCHAR NOT NULL,   -- peer_fund | confirmed_lp | query_template
                value           VARCHAR NOT NULL,
                geography       VARCHAR,
                enabled         BOOLEAN NOT NULL DEFAULT TRUE,
                origin          VARCHAR NOT NULL DEFAULT 'default',  -- default | expansion:<name> | manual
                last_mined_at   TIMESTAMPTZ,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_prospector_seeds_value "
            "ON prospector_seeds(seed_type, value)"
        )
        ran = True
    if not _table_exists(con, "prospector_candidates"):
        con.execute(
            """
            CREATE TABLE prospector_candidates (
                candidate_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                investor_name   VARCHAR NOT NULL,
                name_key        VARCHAR NOT NULL UNIQUE,
                entity_type     VARCHAR,
                geography       VARCHAR,
                discovery_evidence VARCHAR,      -- the snippet that surfaced them
                source_url      VARCHAR,
                run_id          VARCHAR,
                seed            VARCHAR,          -- which seed/query found them
                status          VARCHAR NOT NULL DEFAULT 'review',
                    -- qualified | review | rejected | promoted | dismissed
                verdict_reason  VARCHAR,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_prospector_candidates_status "
            "ON prospector_candidates(status)"
        )
        ran = True
    if not _table_exists(con, "prospector_runs"):
        con.execute(
            """
            CREATE TABLE prospector_runs (
                run_id          VARCHAR PRIMARY KEY,
                status          VARCHAR NOT NULL DEFAULT 'running',
                    -- running | completed | failed
                trigger         VARCHAR NOT NULL DEFAULT 'manual',   -- manual | scheduled
                seeds_json      JSON,
                queries_used    INTEGER NOT NULL DEFAULT 0,
                results_seen    INTEGER NOT NULL DEFAULT 0,
                candidates_found INTEGER NOT NULL DEFAULT 0,
                new_candidates  INTEGER NOT NULL DEFAULT 0,
                qualified       INTEGER NOT NULL DEFAULT 0,
                review          INTEGER NOT NULL DEFAULT 0,
                rejected        INTEGER NOT NULL DEFAULT 0,
                promoted        INTEGER NOT NULL DEFAULT 0,
                seeds_added     INTEGER NOT NULL DEFAULT 0,
                error           VARCHAR,
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at    TIMESTAMPTZ
            )
            """
        )
        ran = True
    return ran


def migrate_prospector_cascade(con) -> bool:
    """
    Add the 5-stage cascade columns to the Prospector tables.

    The miner is a cascade: each stage is cheaper than the next and rejects on a
    different failure mode, so the expensive LP gate only ever sees survivors.

        1 HARVEST      document-level discovery; many LPs per fetched page
        2 RESOLVE      identity + non-entity rejection (zero API)
        3 PRERANK      structural disqualifiers + ranking (zero API)
        4 CORROBORATE  independent-domain commitment quote (1 search each)
        5 ADJUDICATE   full LP gate

    prospector_runs gains one counter per stage so a run that yields no leads
    reports exactly which stage the funnel died at, instead of being a mystery.

    prospector_candidates gains the evidence each stage produces: the verbatim
    span and domain that surfaced the name (Stage 1), the prerank score and which
    ICP checks fired (Stage 3), and the independent corroborating quotes (Stage 4).
    Storing spans rather than paraphrases is what lets Stages 3-5 test against
    literal text.

    Safe to re-run: ALTER TABLE ... ADD COLUMN IF NOT EXISTS.

    NOTE: no NOT NULL on these columns. DuckDB rejects ADD COLUMN with any
    constraint ("Adding columns with constraints not yet supported"), and a
    single raised migration aborts every migration after it. DEFAULT alone still
    backfills existing rows, and the writers here always supply a value.
    """
    ran = False

    if _table_exists(con, "prospector_runs"):
        existing = {
            r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'prospector_runs'"
            ).fetchall()
        }
        for col, ddl in [
            ("harvested",    "INTEGER DEFAULT 0"),
            ("docs_fetched", "INTEGER DEFAULT 0"),
            ("resolved",     "INTEGER DEFAULT 0"),
            ("preranked",    "INTEGER DEFAULT 0"),
            ("corroborated", "INTEGER DEFAULT 0"),
            ("gated",        "INTEGER DEFAULT 0"),
        ]:
            if col not in existing:
                con.execute(
                    f"ALTER TABLE prospector_runs ADD COLUMN IF NOT EXISTS {col} {ddl}"
                )
                ran = True

    if _table_exists(con, "prospector_candidates"):
        existing = {
            r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'prospector_candidates'"
            ).fetchall()
        }
        for col, ddl in [
            ("source_domain",       "VARCHAR"),
            ("stage",               "VARCHAR"),   # harvest|resolve|prerank|corroborate|gate
            ("prerank_score",       "INTEGER"),
            ("prerank_checks_json", "JSON"),
            ("source_diversity",    "INTEGER DEFAULT 0"),
            ("corroborated",        "BOOLEAN DEFAULT FALSE"),
            ("corroboration_json",  "JSON"),
            ("gate_verdict",        "VARCHAR"),
            ("revisit_date",        "DATE"),
        ]:
            if col not in existing:
                con.execute(
                    f"ALTER TABLE prospector_candidates ADD COLUMN IF NOT EXISTS {col} {ddl}"
                )
                ran = True

    return ran


def migrate_prospector_cost(con) -> bool:
    """
    Add cost / duration columns to prospector_runs for spend monitoring.

    search_calls / llm_calls / fetch_calls / gate_calls — billable unit counts
    estimated_cost_usd — rough USD using PROSPECTOR_COST_*_USD unit prices
    duration_sec — wall-clock length of the run
    cost_json — full meter snapshot (unit prices + counts) for audit

    Safe to re-run: ADD COLUMN IF NOT EXISTS.
    """
    if not _table_exists(con, "prospector_runs"):
        return False

    existing = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'prospector_runs'"
        ).fetchall()
    }
    ran = False
    for col, ddl in [
        ("search_calls", "INTEGER DEFAULT 0"),
        ("llm_calls", "INTEGER DEFAULT 0"),
        ("fetch_calls", "INTEGER DEFAULT 0"),
        ("gate_calls", "INTEGER DEFAULT 0"),
        ("estimated_cost_usd", "DOUBLE DEFAULT 0"),
        ("duration_sec", "DOUBLE DEFAULT 0"),
        ("cost_json", "JSON"),
        ("current_stage", "VARCHAR"),  # live progress: harvest|resolve|…|gate
    ]:
        if col not in existing:
            con.execute(
                f"ALTER TABLE prospector_runs ADD COLUMN IF NOT EXISTS {col} {ddl}"
            )
            ran = True
    return ran


def migrate_prospector_search_diag(con) -> bool:
    """
    Add per-run search outcome columns to prospector_runs.

    `results_seen = 0` has three very different causes — the provider raised on
    every query, the provider answered but found nothing, or no query ran — and
    they need opposite fixes. Without these counts a dead run is indistinguishable
    from a misconfigured host, which is exactly how 13 consecutive zero-lead runs
    went undiagnosed.

    Safe to re-run: ADD COLUMN IF NOT EXISTS.
    """
    if not _table_exists(con, "prospector_runs"):
        return False

    existing = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'prospector_runs'"
        ).fetchall()
    }
    ran = False
    for col, ddl in [
        ("search_ok", "INTEGER DEFAULT 0"),       # queries that returned >=1 result
        ("search_empty", "INTEGER DEFAULT 0"),    # queries the provider answered with nothing
        ("search_errors", "INTEGER DEFAULT 0"),   # queries that raised
        ("search_provider", "VARCHAR"),
    ]:
        if col not in existing:
            con.execute(
                f"ALTER TABLE prospector_runs ADD COLUMN IF NOT EXISTS {col} {ddl}"
            )
            ran = True
    return ran


def migrate_web_search_log(con) -> bool:
    """
    Add web_search_log — audit trail for every Anthropic/OpenAI/Tavily search.

    Without this, provider dashboards show spend (e.g. 809 web searches) but the
    app has no recoverable record of what was queried or which URLs came back.
    Gate sessions only hold web_context in memory for ~30 minutes.
    """
    if _table_exists(con, "web_search_log"):
        return False
    con.execute(
        """
        CREATE TABLE web_search_log (
            log_id          VARCHAR PRIMARY KEY,
            provider        VARCHAR NOT NULL,
            source          VARCHAR NOT NULL DEFAULT 'unknown',
                -- gate | prospector | discovery | other
            query           VARCHAR NOT NULL,
            result_count    INTEGER NOT NULL DEFAULT 0,
            urls_json       JSON,
            cached          BOOLEAN NOT NULL DEFAULT FALSE,
            error           VARCHAR,
            duration_ms     DOUBLE,
            max_results     INTEGER,
            investor_name   VARCHAR,
            run_id          VARCHAR,
            session_id      VARCHAR,
            meta_json       JSON,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_log_created "
        "ON web_search_log(created_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_search_log_source "
        "ON web_search_log(source)"
    )
    return True


def migrate_outreach_log(con) -> bool:
    """
    Add outreach_log — the historical record of who we already emailed.

    Backfilled from past_outreach/*.eml (one row per recipient email per send)
    and appended to when Gmail drafts are marked sent. Deduped by (email, sent_at).
    The Prospector consults recipient name_keys here so it never re-surfaces
    someone we already contacted, and the funnel counts contacted from it.
    """
    if _table_exists(con, "outreach_log"):
        return False
    con.execute(
        """
        CREATE TABLE outreach_log (
            log_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            recipient_email VARCHAR NOT NULL,
            recipient_name  VARCHAR,
            name_key        VARCHAR,
            company_domain  VARCHAR,
            subject         VARCHAR,
            sent_at         TIMESTAMPTZ,
            source          VARCHAR NOT NULL DEFAULT 'eml_backfill',
            source_file     VARCHAR,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outreach_log_email ON outreach_log(recipient_email)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outreach_log_name_key ON outreach_log(name_key)"
    )
    return True


def migrate_allocator_contacts_v2(con) -> bool:
    """
    Add twitter_url and channels_json to allocator_contacts.

    twitter_url  — X/Twitter profile URL (x.com/... or twitter.com/...)
    channels_json — structured list of all contact channels with source + confidence:
        [{"type":"email","value":"...","source":"gate_research","confidence":0.85}, ...]

    Runs as ALTER TABLE ... ADD COLUMN IF NOT EXISTS so it is safe to re-run.
    Returns True if any column was added.
    """
    ran = False
    existing_cols = {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'allocator_contacts'"
        ).fetchall()
    }
    if "twitter_url" not in existing_cols:
        con.execute("ALTER TABLE allocator_contacts ADD COLUMN IF NOT EXISTS twitter_url VARCHAR")
        ran = True
    if "channels_json" not in existing_cols:
        con.execute("ALTER TABLE allocator_contacts ADD COLUMN IF NOT EXISTS channels_json JSON")
        ran = True
    return ran
