"""
pulse.exports.outreach_pack
===========================
Generates two export CSVs from the live DuckDB:

  1. First_LPs_Ready.csv      — full enriched list (institutional prospects, wide format)
  2. First_LPs_Outreach_Pack.csv — partner-facing pack:
       Section A  ICP Tier 1 — Client Approved   (institutional xlsx names)

Replaces: scripts/export_first_lps_ready.py  and  scripts/export_outreach_pack.py
Called by: pulse/orchestrator.py  and (on demand) pulse explore UI.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
PROCESSED = ROOT / "processed_data"

_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(ltd|limited|llc|inc|corp|plc|pte|sa|bv|gmbh|lp|llp|co)\.?$", re.IGNORECASE
)

BLOCKLIST: frozenset = frozenset({"vts", "acrew capital"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    key = (name or "").strip().lower()
    key = _LEGAL_SUFFIX_RE.sub("", key).strip()
    return key


def _load_defaults() -> Dict[str, Any]:
    import yaml
    path = ROOT / "prompts" / "pulse_defaults.yaml"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _load_connectivity(con) -> Dict[str, Dict]:
    """Pull connectivity columns from the DB signals table."""
    rows = con.execute(
        """
        SELECT
            CAST(a.canonical_name AS VARCHAR) AS name,
            s.signal_type,
            s.normalized_value
        FROM signals s
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(s.allocator_id AS VARCHAR)
        WHERE s.signal_type IN ('bridge_strength', 'warm_path_count', 'network_density',
                                'social_proximity')
        """
    ).fetchall()
    result: Dict[str, Dict] = {}
    for name, sig_type, val in rows:
        result.setdefault(_norm_key(name), {})[sig_type] = val

    csv_path = PROCESSED / "Prospect_Syndicate_Connectivity.csv"
    if csv_path.exists():
        try:
            conn_rows = con.execute(
                "SELECT canonical_name, connectivity_score, direct_syndicate_degree, "
                "two_hop_syndicate_reach, top_bridge_name "
                f"FROM read_csv_auto('{csv_path.as_posix()}')"
            ).fetchall()
            for cname, score, degree, two_hop, bridge in conn_rows:
                k = _norm_key(cname or "")
                result.setdefault(k, {}).update({
                    "connectivity_score": score,
                    "direct_syndicate_degree": degree,
                    "two_hop_syndicate_reach": two_hop,
                    "top_bridge_name": bridge or "",
                })
        except Exception:
            pass

    return result


def _load_xlsx_contacts(con) -> Dict[str, Dict]:
    """Extract contact columns from prospect xlsx raw rows."""
    rows = con.execute(
        """
        SELECT raw_content FROM entities_raw
        WHERE source_type = 'xlsx'
        """
    ).fetchall()
    result: Dict[str, Dict] = {}
    for (raw,) in rows:
        if isinstance(raw, str):
            raw = json.loads(raw)
        name = (raw.get("Unnamed: 1") or "").strip()
        if not name:
            continue
        k = _norm_key(name)
        if k not in result:
            result[k] = {
                "email": raw.get("Unnamed: 16") or "",
                "linkedin": raw.get("Unnamed: 17") or "",
                "website": raw.get("Unnamed: 3") or "",
                "contact_name": raw.get("Unnamed: 13") or "",
                "phone": "",
            }
    return result


def _safe_csv_write(path: Path, rows_data, fieldnames) -> None:
    """Write CSV via temp-then-rename so Excel locks don't abort the pipeline."""
    import os
    import shutil
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=path.parent, prefix="_pulse_tmp_")
    try:
        os.close(fd)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows_data)
        try:
            os.replace(tmp, path)
        except PermissionError:
            from datetime import datetime
            fallback = path.with_stem(path.stem + "_" + datetime.now().strftime("%H%M%S"))
            shutil.move(tmp, fallback)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# First_LPs_Ready.csv  (rich analyst export — institutional prospects)
# ---------------------------------------------------------------------------

READY_FIELDS = [
    "priority_rank", "data_source", "readiness", "lp_name",
    "allocator_type", "geography", "hq_country",
    "icp_tier", "fit_score", "client_decision", "client_status",
    "core_pass", "c1_vc_fund", "c2_emerging_manager", "c3_ai_tech", "c4_geography",
    "s1_ai_signal", "s2_em_signal", "s3_lp_type_score",
    "connectivity_score", "bridge_strength", "warm_path_count",
    "network_density", "syndicate_degree", "two_hop_reach", "top_bridge_lp",
    "contact_name", "contact_email", "contact_phone", "contact_linkedin", "website",
    "location_detail", "industry_focus", "exclusion_reason", "stated_reason",
    "source_file", "pipeline_notes", "allocator_id",
]


def generate_first_lps_ready(con, out_path: Optional[Path] = None) -> Dict[str, Any]:
    """Generate First_LPs_Ready.csv from institutional prospects."""
    out_path = out_path or PROCESSED / "First_LPs_Ready.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    connectivity = _load_connectivity(con)
    xlsx_contacts = _load_xlsx_contacts(con)

    institutional = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR) AS allocator_id,
            a.canonical_name, a.allocator_type, a.geography, a.hq_country,
            i.fit_score, i.tier, i.core_pass, i.excluded,
            i.client_status, i.client_decision,
            i.c1_asset_class_pass, i.c2_emerging_manager_pass,
            i.c3_ai_tech_pass, i.c4_geography_pass,
            i.s1_ai_signal, i.s2_emerging_manager, i.s3_lp_type,
            i.exclusion_reason, i.stated_reason, i.source_file
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE COALESCE(a.population, '') = 'institutional_prospect'
          AND i.excluded = false
          AND i.core_pass = true
        ORDER BY
            CASE i.tier WHEN 'tier_1' THEN 1 WHEN 'tier_2' THEN 2 WHEN 'tier_3' THEN 3 ELSE 4 END,
            i.fit_score DESC
        """
    ).fetchall()

    cols = [
        "allocator_id", "canonical_name", "allocator_type", "geography", "hq_country",
        "fit_score", "tier", "core_pass", "excluded", "client_status", "client_decision",
        "c1_asset_class_pass", "c2_emerging_manager_pass", "c3_ai_tech_pass", "c4_geography_pass",
        "s1_ai_signal", "s2_emerging_manager", "s3_lp_type",
        "exclusion_reason", "stated_reason", "source_file",
    ]

    rows_out: List[Dict] = []
    rank = 0
    seen_ids: set = set()

    def _add(source: str, readiness: str, row_tuple: tuple, notes: str = "") -> None:
        nonlocal rank
        d = dict(zip(cols, row_tuple))
        aid = d["allocator_id"]
        if aid in seen_ids:
            return
        name = d["canonical_name"] or ""
        if _norm_key(name) in BLOCKLIST:
            return
        seen_ids.add(aid)
        rank += 1
        k = _norm_key(name)
        conn = connectivity.get(k, {})
        contact = xlsx_contacts.get(k) or {}
        rows_out.append({
            "priority_rank": rank,
            "data_source": source,
            "readiness": readiness,
            "lp_name": name,
            "allocator_type": d["allocator_type"] or "",
            "geography": d["geography"] or "",
            "hq_country": d["hq_country"] or "",
            "icp_tier": d["tier"],
            "fit_score": round(float(d["fit_score"] or 0), 3),
            "client_decision": d["client_decision"] or "",
            "client_status": d["client_status"] or "",
            "core_pass": d["core_pass"],
            "c1_vc_fund": d["c1_asset_class_pass"],
            "c2_emerging_manager": d["c2_emerging_manager_pass"],
            "c3_ai_tech": d["c3_ai_tech_pass"],
            "c4_geography": d["c4_geography_pass"],
            "s1_ai_signal": round(float(d["s1_ai_signal"] or 0), 2),
            "s2_em_signal": round(float(d["s2_emerging_manager"] or 0), 2),
            "s3_lp_type_score": round(float(d["s3_lp_type"] or 0), 2),
            "connectivity_score": conn.get("connectivity_score", conn.get("network_density", "")),
            "bridge_strength": conn.get("bridge_strength", ""),
            "warm_path_count": conn.get("warm_path_count", ""),
            "network_density": conn.get("network_density", ""),
            "syndicate_degree": conn.get("direct_syndicate_degree", ""),
            "two_hop_reach": conn.get("two_hop_syndicate_reach", ""),
            "top_bridge_lp": conn.get("top_bridge_name", ""),
            "contact_name": contact.get("contact_name", ""),
            "contact_email": contact.get("email", ""),
            "contact_phone": contact.get("phone", ""),
            "contact_linkedin": contact.get("linkedin", ""),
            "website": contact.get("website", ""),
            "location_detail": contact.get("location", ""),
            "industry_focus": contact.get("industry", ""),
            "exclusion_reason": d["exclusion_reason"] or "",
            "stated_reason": (d["stated_reason"] or "")[:200],
            "source_file": d["source_file"] or contact.get("source_file", ""),
            "pipeline_notes": notes,
            "allocator_id": aid,
        })

    for row in institutional:
        d = dict(zip(cols, row))
        tier = d["tier"]
        client_dec = (d["client_decision"] or "").lower()
        if tier == "tier_1":
            _add("ICP Prospect List (xlsx)", "READY — Tier 1, client approved", row)
        elif tier == "tier_2" and "approved" in client_dec:
            _add("ICP Prospect List (xlsx)", "READY — Tier 2, client approved", row)
        elif tier == "tier_2" and float(d["fit_score"] or 0) >= 0.60:
            _add(
                "ICP Prospect List (xlsx)",
                "NEAR-READY — Tier 2, strong fit; needs client approval",
                row,
                "Confirm EM appetite before outreach",
            )

    _safe_csv_write(out_path, rows_out, READY_FIELDS)

    return {
        "rows_total": len(rows_out),
        "tier1_institutional": sum(1 for r in rows_out if r["icp_tier"] == "tier_1" and r["data_source"].startswith("ICP")),
        "tier2_institutional": sum(1 for r in rows_out if r["icp_tier"] == "tier_2" and r["data_source"].startswith("ICP")),
        "out_path": str(out_path),
    }


# ---------------------------------------------------------------------------
# First_LPs_Outreach_Pack.csv  (compact partner-facing pack)
# ---------------------------------------------------------------------------

PACK_FIELDS = [
    "pack_section", "priority", "lp_name", "type", "geography",
    "tier", "fit_score", "client_status", "decision",
    "email", "phone", "linkedin", "contact_name", "website",
    "connectivity_score", "syndicate_degree", "two_hop_reach", "top_bridge",
    "em_signal", "geo_overlap", "data_source", "notes",
]


def generate_outreach_pack(con, out_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Generate First_LPs_Outreach_Pack.csv.

    Section A — ICP Tier 1 Client Approved (institutional xlsx only).
    """
    out_path = out_path or PROCESSED / "First_LPs_Outreach_Pack.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    connectivity = _load_connectivity(con)
    xlsx_contacts = _load_xlsx_contacts(con)

    section_a_rows = con.execute(
        """
        SELECT
            CAST(i.allocator_id AS VARCHAR),
            a.canonical_name, a.allocator_type, a.geography, i.fit_score, i.tier,
            i.client_status, i.client_decision,
            i.s2_emerging_manager, i.s5_stage
        FROM icp_scores i
        JOIN allocators a ON CAST(a.allocator_id AS VARCHAR) = CAST(i.allocator_id AS VARCHAR)
        WHERE COALESCE(a.population, '') = 'institutional_prospect'
          AND i.tier = 'tier_1'
          AND LOWER(COALESCE(i.client_decision, '')) LIKE '%approved%'
          AND i.excluded = false
        ORDER BY i.fit_score DESC
        """
    ).fetchall()

    out_rows: List[Dict] = []
    prio = 0

    for row in section_a_rows:
        aid, name, atype, geo, fit, tier, cl_status, cl_dec, s2, s5 = row
        if _norm_key(name) in BLOCKLIST:
            continue
        k = _norm_key(name)
        conn = connectivity.get(k, {})
        contact = xlsx_contacts.get(k) or {}
        prio += 1
        out_rows.append({
            "pack_section": "ICP Tier 1 — Client Approved",
            "priority": prio,
            "lp_name": name,
            "type": atype or "",
            "geography": geo or "",
            "tier": tier,
            "fit_score": round(float(fit or 0), 3),
            "client_status": cl_status or "",
            "decision": cl_dec or "",
            "email": contact.get("email", ""),
            "phone": contact.get("phone", ""),
            "linkedin": contact.get("linkedin", ""),
            "contact_name": contact.get("contact_name", ""),
            "website": contact.get("website", ""),
            "connectivity_score": conn.get("connectivity_score", conn.get("network_density", "")),
            "syndicate_degree": conn.get("direct_syndicate_degree", ""),
            "two_hop_reach": conn.get("two_hop_syndicate_reach", ""),
            "top_bridge": conn.get("top_bridge_name", ""),
            "em_signal": round(float(s2 or 0), 2) if s2 is not None else "",
            "geo_overlap": round(float(s5 or 0), 2) if s5 is not None else "",
            "data_source": "prospect_sheet",
            "notes": "",
        })

    _safe_csv_write(out_path, out_rows, PACK_FIELDS)

    return {
        "rows_total": len(out_rows),
        "section_a_tier1_approved": len(out_rows),
        "out_path": str(out_path),
    }


def run_all_exports(con) -> Dict[str, Any]:
    """Generate both CSVs. Called by the orchestrator after calibrate."""
    ready_stats = generate_first_lps_ready(con)
    pack_stats = generate_outreach_pack(con)
    return {
        "first_lps_ready": ready_stats,
        "outreach_pack": pack_stats,
    }
