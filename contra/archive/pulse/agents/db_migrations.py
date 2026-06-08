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
