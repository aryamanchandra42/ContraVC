"""
Read-only DuckDB queries for the PULSE LP Explorer.

Reuses SQL patterns from calibration.export_lp_ranked_list and
scripts/export_outreach_pack without duplicating scoring logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd

from agents.scoring.icp_spec import ICP_VERSION

ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED = ROOT / "processed_data"
CONNECTIVITY_CSV = PROCESSED / "Prospect_Syndicate_Connectivity.csv"
DB_PATH = ROOT / "pulse.duckdb"


@dataclass
class OutreachFilters:
    populations: Optional[List[str]] = None
    tiers: Optional[List[str]] = None
    min_fit_score: float = 0.0
    name_search: str = ""
    tier1_approved_only: bool = False
    has_email_only: bool = False
    institutional_only: bool = True


def db_path() -> Path:
    return DB_PATH


def connectivity_csv_exists() -> bool:
    return CONNECTIVITY_CSV.exists()


def last_pipeline_run(con: duckdb.DuckDBPyConnection) -> Optional[Dict[str, Any]]:
    try:
        row = con.execute(
            """
            SELECT stage, status, started_at, completed_at
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {
        "stage": row[0],
        "status": row[1],
        "started_at": row[2],
        "completed_at": row[3],
    }


def funnel_metrics(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """Table counts, population split, ICP tier/source breakdown."""
    metrics: Dict[str, Any] = {"icp_version": ICP_VERSION}

    for tbl in (
        "entities_raw",
        "allocators",
        "relationships",
        "relationship_evidence",
        "icp_scores",
        "signals",
    ):
        try:
            metrics[f"count_{tbl}"] = con.execute(
                f"SELECT COUNT(*) FROM {tbl}"
            ).fetchone()[0]
        except Exception:
            metrics[f"count_{tbl}"] = None

    try:
        pop_rows = con.execute(
            """
            SELECT COALESCE(population, 'null') AS pop, COUNT(*) AS cnt
            FROM allocators
            GROUP BY pop
            ORDER BY cnt DESC
            """
        ).fetchdf()
        metrics["population_split"] = pop_rows.to_dict("records")
    except Exception:
        metrics["population_split"] = []

    try:
        tier_rows = con.execute(
            """
            SELECT COALESCE(tier, 'null') AS tier, COUNT(*) AS cnt
            FROM icp_scores
            WHERE icp_version = ?
            GROUP BY tier
            ORDER BY tier
            """,
            [ICP_VERSION],
        ).fetchdf()
        metrics["tier_split"] = tier_rows.to_dict("records")
    except Exception:
        metrics["tier_split"] = []

    try:
        source_rows = con.execute(
            """
            SELECT COALESCE(source_sheet, 'unknown') AS source_sheet, COUNT(*) AS cnt
            FROM icp_scores
            WHERE icp_version = ?
            GROUP BY source_sheet
            ORDER BY cnt DESC
            """,
            [ICP_VERSION],
        ).fetchdf()
        metrics["source_split"] = source_rows.to_dict("records")
    except Exception:
        metrics["source_split"] = []

    metrics["csv_lp_ranked_rows"] = _csv_row_count(PROCESSED / "LP_Ranked_List.csv")
    metrics["csv_outreach_pack_rows"] = _csv_row_count(
        PROCESSED / "First_LPs_Outreach_Pack.csv"
    )

    return metrics


def _csv_row_count(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    try:
        return len(pd.read_csv(path))
    except Exception:
        return None


def _load_connectivity_df() -> pd.DataFrame:
    if not CONNECTIVITY_CSV.exists():
        return pd.DataFrame()
    return pd.read_csv(CONNECTIVITY_CSV)


def _ranked_institutional_df(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """ICP-ranked allocators with signal + connectivity columns (live from DB)."""
    rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR) AS allocator_id,
            a.canonical_name,
            a.allocator_type,
            a.geography,
            a.population,
            i.tier,
            i.fit_score,
            i.client_status,
            i.client_decision,
            i.stated_reason,
            i.data_miner_comment,
            i.source_sheet,
            s_em.normalized_value AS em_participation,
            s_geo.normalized_value AS geo_overlap
        FROM icp_scores i
        JOIN allocators a
            ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        LEFT JOIN signals s_em
            ON CAST(s_em.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_em.signal_type = 'em_participation'
        LEFT JOIN signals s_geo
            ON CAST(s_geo.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
            AND s_geo.signal_type = 'geography_overlap'
        WHERE i.icp_version = ?
        ORDER BY i.fit_score DESC NULLS LAST
        """,
        [ICP_VERSION],
    ).fetchdf()

    if rows.empty:
        return rows

    conn_df = _load_connectivity_df()
    if not conn_df.empty:
        rows = rows.merge(
            conn_df[
                [
                    "allocator_id",
                    "connectivity_score",
                    "direct_syndicate_degree",
                    "two_hop_syndicate_reach",
                    "top_bridge_name",
                ]
            ],
            on="allocator_id",
            how="left",
        )
    else:
        for col in (
            "connectivity_score",
            "direct_syndicate_degree",
            "two_hop_syndicate_reach",
            "top_bridge_name",
        ):
            rows[col] = None

    return rows


def _xlsx_contacts(con: duckdb.DuckDBPyConnection, name: str) -> Dict[str, str]:
    rows = con.execute(
        """
        SELECT raw_content FROM entities_raw
        WHERE source_type = 'xlsx'
          AND raw_content::VARCHAR LIKE ?
        LIMIT 5
        """,
        [f"%{name}%"],
    ).fetchall()
    for (raw,) in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)
        n = (raw.get("Unnamed: 1") or "").strip()
        if n.lower() != name.lower().strip():
            continue
        return {
            "email": (raw.get("Unnamed: 16") or "").strip(),
            "linkedin": (raw.get("Unnamed: 17") or "").strip(),
            "website": (raw.get("Unnamed: 3") or "").strip(),
            "contact_name": (raw.get("Unnamed: 13") or "").strip(),
            "phone": "",
        }
    return {}


def outreach_queue(
    con: duckdb.DuckDBPyConnection,
    filters: OutreachFilters,
) -> pd.DataFrame:
    """Live outreach table: ICP-ranked institutional rows."""
    ranked = _ranked_institutional_df(con)
    rows: List[Dict[str, Any]] = []

    if not ranked.empty:
        for _, r in ranked.iterrows():
            name = r["canonical_name"]
            contacts = _xlsx_contacts(con, name)
            decision = (r.get("client_decision") or "") or ""
            rows.append({
                "pack_section": "ICP ranked",
                "allocator_id": r["allocator_id"],
                "lp_name": name,
                "type": r.get("allocator_type", ""),
                "geography": r.get("geography", ""),
                "population": r.get("population", ""),
                "tier": r.get("tier", ""),
                "fit_score": r.get("fit_score"),
                "client_status": r.get("client_status", ""),
                "decision": decision,
                "email": contacts.get("email", ""),
                "phone": contacts.get("phone", ""),
                "linkedin": contacts.get("linkedin", ""),
                "contact_name": contacts.get("contact_name", ""),
                "website": contacts.get("website", ""),
                "connectivity_score": r.get("connectivity_score"),
                "syndicate_degree": r.get("direct_syndicate_degree"),
                "two_hop_reach": r.get("two_hop_syndicate_reach"),
                "top_bridge": r.get("top_bridge_name", ""),
                "em_signal": r.get("em_participation"),
                "geo_overlap": r.get("geo_overlap"),
                "source_sheet": r.get("source_sheet", ""),
                "notes": (r.get("data_miner_comment") or r.get("stated_reason") or "")[:200],
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = _apply_outreach_filters(df, filters)
    if "fit_score" in df.columns:
        df = df.sort_values("fit_score", ascending=False, na_position="last")
    return df.reset_index(drop=True)


def _apply_outreach_filters(df: pd.DataFrame, f: OutreachFilters) -> pd.DataFrame:
    if df.empty:
        return df

    if f.institutional_only:
        df = df[df["population"] == "institutional_prospect"]

    if f.populations:
        df = df[df["population"].isin(f.populations) | df["population"].isna()]

    if f.tiers:
        df = df[df["tier"].isin(f.tiers)]

    if f.min_fit_score > 0 and "fit_score" in df.columns:
        scores = pd.to_numeric(df["fit_score"], errors="coerce").fillna(0)
        df = df[scores >= f.min_fit_score]

    if f.name_search.strip():
        q = f.name_search.strip().lower()
        df = df[df["lp_name"].str.lower().str.contains(q, na=False)]

    if f.tier1_approved_only:
        df = df[
            (df["tier"] == "tier_1")
            & (df["decision"].astype(str).str.lower().str.contains("approved", na=False))
        ]

    if f.has_email_only:
        df = df[df["email"].astype(str).str.strip() != ""]

    return df


def list_scored_allocators(
    con: duckdb.DuckDBPyConnection,
    population: str = "institutional_prospect",
) -> pd.DataFrame:
    return con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR) AS allocator_id,
            a.canonical_name,
            i.tier,
            i.fit_score
        FROM icp_scores i
        JOIN allocators a
            ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE i.icp_version = ?
          AND (a.population = ? OR ? = '')
        ORDER BY i.fit_score DESC NULLS LAST
        """,
        [ICP_VERSION, population or "", population or ""],
    ).fetchdf()


def allocator_detail(
    con: duckdb.DuckDBPyConnection,
    allocator_id: str,
) -> Dict[str, Any]:
    """ICP gates, signals, contacts, and raw source snippet for one allocator."""
    icp = con.execute(
        """
        SELECT i.*, a.canonical_name, a.allocator_type, a.geography, a.population
        FROM icp_scores i
        JOIN allocators a
            ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE CAST(i.allocator_id AS VARCHAR) = ?
          AND i.icp_version = ?
        LIMIT 1
        """,
        [allocator_id, ICP_VERSION],
    ).fetchdf()

    signals = con.execute(
        """
        SELECT signal_type, normalized_value, confidence, source_file
        FROM signals
        WHERE CAST(allocator_id AS VARCHAR) = ?
        ORDER BY signal_type
        """,
        [allocator_id],
    ).fetchdf()

    name = ""
    if not icp.empty:
        name = icp.iloc[0]["canonical_name"]

    contacts = _xlsx_contacts(con, name) if name else {}

    raw_rows = con.execute(
        """
        SELECT source_file, source_offset, source_type, raw_content, ingested_at
        FROM entities_raw
        WHERE raw_content::VARCHAR LIKE ?
        ORDER BY ingested_at DESC
        LIMIT 5
        """,
        [f"%{name}%"] if name else ["%"],
    ).fetchdf()

    raw_snippets: List[Dict[str, Any]] = []
    for _, row in raw_rows.iterrows():
        raw = row["raw_content"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {"_raw": raw[:2000]}
        snippet = dict(raw) if isinstance(raw, dict) else {"_raw": str(raw)[:2000]}
        raw_snippets.append({
            "source_file": row["source_file"],
            "source_offset": row["source_offset"],
            "source_type": row["source_type"],
            "ingested_at": row["ingested_at"],
            "snippet": snippet,
        })

    return {
        "icp": icp.to_dict("records")[0] if not icp.empty else {},
        "signals": signals.to_dict("records"),
        "contacts": contacts,
        "raw_snippets": raw_snippets,
    }


def connectivity_for(
    con: duckdb.DuckDBPyConnection,
    allocator_id: str,
) -> Dict[str, Any]:
    conn_df = _load_connectivity_df()
    if conn_df.empty:
        return {}
    match = conn_df[conn_df["allocator_id"].astype(str) == str(allocator_id)]
    if match.empty:
        return {}
    return match.iloc[0].to_dict()


def ego_edges(
    con: duckdb.DuckDBPyConnection,
    allocator_id: str,
    limit: int = 200,
) -> pd.DataFrame:
    """Direct edges incident on allocator from relationships_effective."""
    try:
        return con.execute(
            """
            SELECT
                re.edge_type,
                re.confidence,
                re.temporal_confidence,
                re.weight,
                re.evidence_count,
                CAST(re.source_node_id AS VARCHAR) AS source_node_id,
                CAST(re.target_node_id AS VARCHAR) AS target_node_id,
                COALESCE(sa.canonical_name, CAST(re.source_node_id AS VARCHAR)) AS source_name,
                COALESCE(ta.canonical_name, CAST(re.target_node_id AS VARCHAR)) AS target_name,
                CASE
                    WHEN CAST(re.source_node_id AS VARCHAR) = ?
                        THEN 'outgoing'
                    ELSE 'incoming'
                END AS direction,
                CASE
                    WHEN CAST(re.source_node_id AS VARCHAR) = ?
                        THEN COALESCE(ta.canonical_name, CAST(re.target_node_id AS VARCHAR))
                    ELSE COALESCE(sa.canonical_name, CAST(re.source_node_id AS VARCHAR))
                END AS neighbor_name,
                CASE
                    WHEN CAST(re.source_node_id AS VARCHAR) = ?
                        THEN CAST(re.target_node_id AS VARCHAR)
                    ELSE CAST(re.source_node_id AS VARCHAR)
                END AS neighbor_id
            FROM relationships_effective re
            LEFT JOIN allocators sa
                ON CAST(sa.allocator_id AS VARCHAR) = CAST(re.source_node_id AS VARCHAR)
            LEFT JOIN allocators ta
                ON CAST(ta.allocator_id AS VARCHAR) = CAST(re.target_node_id AS VARCHAR)
            WHERE CAST(re.source_node_id AS VARCHAR) = ?
               OR CAST(re.target_node_id AS VARCHAR) = ?
            ORDER BY re.confidence DESC NULLS LAST
            LIMIT ?
            """,
            [allocator_id] * 5 + [limit],
        ).fetchdf()
    except Exception:
        return pd.DataFrame()
