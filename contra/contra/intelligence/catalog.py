"""Data estate catalog for Contra DB."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent


CATALOG_TABLES = [
    ("allocators", "Canonical LP entities"),
    ("icp_scores", "ICP v4.1 scores"),
    ("crm_contacts", "FundingStack CRM export"),
    ("icp_rules", "LP Scoping ICP rules"),
    ("signals", "Weak signals (16 types)"),
    ("relationships", "Graph edges"),
    ("investments", "LP fund commitments"),
    ("benchmark_rankings", "ContraVC Top 200"),
    ("entities_raw", "Raw ingested chunks"),
    ("funds", "Fund entities"),
    ("rejections", "Stated/inferred rejections"),
]


def refresh_catalog(con) -> None:
    now = datetime.now(timezone.utc)
    for key, desc in CATALOG_TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {key}").fetchone()[0]
        except Exception:
            n = 0
        con.execute(
            """
            INSERT INTO data_catalog (catalog_key, description, row_count, last_refreshed)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (catalog_key) DO UPDATE SET
                row_count = excluded.row_count,
                last_refreshed = excluded.last_refreshed
            """,
            [key, desc, n, now],
        )

    raw_files = list((ROOT / "raw_data").glob("*"))
    sources = [f.name for f in raw_files if f.is_file() and f.name != "manifest.json"]
    con.execute(
        """
        INSERT INTO data_catalog (catalog_key, description, row_count, source_files, last_refreshed)
        VALUES ('raw_data_sources', 'Immutable source files', ?, ?, ?)
        ON CONFLICT (catalog_key) DO UPDATE SET
            row_count = excluded.row_count,
            source_files = excluded.source_files,
            last_refreshed = excluded.last_refreshed
        """,
        [len(sources), json.dumps(sources), now],
    )


def get_catalog(con) -> Dict[str, Any]:
    rows = con.execute(
        "SELECT catalog_key, description, row_count, source_files, last_refreshed FROM data_catalog ORDER BY catalog_key"
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append({
            "key": r[0],
            "description": r[1],
            "row_count": r[2],
            "source_files": json.loads(r[3]) if r[3] else None,
            "last_refreshed": str(r[4]) if r[4] else None,
        })

    pop = con.execute(
        """
        SELECT population, COUNT(*) FROM allocators GROUP BY population
        """
    ).fetchall()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": items,
        "allocator_populations": {p or "unknown": c for p, c in pop},
    }
