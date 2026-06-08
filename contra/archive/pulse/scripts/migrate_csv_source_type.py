"""
Migration: add 'csv' to the entities_raw source_type CHECK constraint.
Safe to re-run from any point.
"""
import duckdb
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "pulse.duckdb"
VIEWS_SQL = ROOT / "schema" / "views.sql"

con = duckdb.connect(str(DB_PATH))

def p(msg):
    # Windows-safe print
    print(msg.encode("ascii", "replace").decode("ascii"))

# --- Check current state ---
tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
p(f"Tables present: {sorted(tables)}")

# Check if backup exists (means rename already happened)
backup_exists = "entities_raw_backup" in tables
main_exists   = "entities_raw" in tables

if backup_exists and not main_exists:
    p("Resuming: backup exists, main table missing. Recreating entities_raw...")
elif backup_exists and main_exists:
    p("Both tables exist — dropping backup and starting fresh from main.")
    con.execute("DROP TABLE entities_raw_backup")
    backup_exists = False

# Check if migration is needed
if main_exists:
    try:
        con.execute("""
            INSERT INTO entities_raw
                (source_record_id, source_file, source_type, source_offset, content_hash, schema_version)
            VALUES ('__csv_probe__', '__probe__', 'csv', 'probe:0', 'probe', '1.0')
        """)
        con.execute("DELETE FROM entities_raw WHERE source_record_id = '__csv_probe__'")
        p("Migration already applied (csv is accepted). Rebuilding views just in case...")
        # Still rebuild views in case they were dropped earlier
        if VIEWS_SQL.exists():
            for stmt in [s.strip() for s in VIEWS_SQL.read_text(encoding="utf-8").split(";") if s.strip()]:
                try:
                    con.execute(stmt)
                except Exception as e:
                    p(f"  View stmt warning: {str(e)[:80]}")
        p("Done.")
        con.close()
        exit(0)
    except Exception:
        pass  # Constraint still rejects csv

if main_exists and not backup_exists:
    # Drop indexes
    for (idx_name,) in con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'entities_raw'"
    ).fetchall():
        try:
            con.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
            p(f"  Dropped index: {idx_name}")
        except Exception as e:
            p(f"  Index drop warn: {idx_name}: {str(e)[:60]}")

    # Drop views
    for (view_name,) in con.execute(
        "SELECT view_name FROM duckdb_views() WHERE internal = false"
    ).fetchall():
        try:
            con.execute(f'DROP VIEW IF EXISTS "{view_name}" CASCADE')
            p(f"  Dropped view: {view_name}")
        except Exception as e:
            p(f"  View drop warn: {view_name}: {str(e)[:60]}")

    # Rename
    try:
        con.execute("ALTER TABLE entities_raw RENAME TO entities_raw_backup")
        p("  Renamed entities_raw to entities_raw_backup")
        backup_exists = True
        main_exists = False
    except Exception as e:
        p(f"  RENAME FAILED: {str(e)[:100]}")
        con.close()
        exit(1)

if backup_exists and not main_exists:
    # Recreate with new constraint
    con.execute("""
        CREATE TABLE entities_raw (
            source_record_id    VARCHAR     PRIMARY KEY,
            source_file         VARCHAR     NOT NULL,
            source_type         VARCHAR     NOT NULL
                CHECK (source_type IN ('xlsx', 'pdf', 'docx', 'api', 'csv')),
            source_offset       VARCHAR     NOT NULL,
            content_hash        VARCHAR     NOT NULL,
            raw_content         JSON,
            ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version      VARCHAR     NOT NULL DEFAULT '1.0'
        )
    """)
    p("  Created entities_raw with updated constraint")

    con.execute("CREATE INDEX idx_entities_raw_source_file  ON entities_raw(source_file)")
    con.execute("CREATE INDEX idx_entities_raw_content_hash ON entities_raw(content_hash)")
    p("  Created indexes")

    count_before = con.execute("SELECT COUNT(*) FROM entities_raw_backup").fetchone()[0]
    con.execute("INSERT INTO entities_raw SELECT * FROM entities_raw_backup")
    count_after  = con.execute("SELECT COUNT(*) FROM entities_raw").fetchone()[0]
    p(f"  Copied {count_after}/{count_before} rows")

    con.execute("DROP TABLE entities_raw_backup")
    p("  Dropped backup")

# Recreate views
if VIEWS_SQL.exists():
    p(f"Recreating views from views.sql...")
    ok = 0
    for stmt in [s.strip() for s in VIEWS_SQL.read_text(encoding="utf-8").split(";") if s.strip()]:
        try:
            con.execute(stmt)
            ok += 1
        except Exception as e:
            p(f"  View warn: {str(e)[:80]}")
    p(f"  {ok} statements executed from views.sql")
else:
    p("  WARNING: schema/views.sql not found")

p("Migration complete. Run: pulse ingest")
con.close()
