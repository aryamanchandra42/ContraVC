"""Populate icp_rules from icp_spec.py (authoritative LP Scoping mirror)."""

from __future__ import annotations

from typing import Dict, List

from agents.scoring import icp_spec


def ingest_icp_rules(con) -> Dict[str, int]:
    rules: List[tuple] = []

    core_rules = [
        ("C1", "VC fund LP", "Must invest in VC funds as LP (not direct-only)", "Core Filters"),
        ("C2", "Emerging manager appetite", "Must back first/second/third-time fund managers", "Core Filters"),
        ("C3", "AI / tech thesis", "AI, deep tech, or technology thesis alignment", "Core Filters"),
        ("C4", "Geography fit", "Asia, North America, Middle East, or global EM appetite", "Core Filters"),
    ]
    for rule_id, name, text, sheet in core_rules:
        rules.append((rule_id, "core", name, text, None, sheet))

    exclusions = [
        ("E1", "PE-only", "Private equity only, no VC fund LP"),
        ("E2", "Secondaries-only", "Secondaries focus only"),
        ("E3", "Real estate only", "Real estate / infra only"),
        ("E4", "Web3-only", "Crypto/web3 only thesis"),
        ("E5", "Healthcare-only", "Healthcare-only mandate"),
        ("E6", "Geo-locked", "Geography locked outside target regions"),
        ("E7", "Impact-only", "Impact-only mandate"),
        ("E8", "No EM evidence", "No emerging markets portfolio evidence"),
        ("E9", "Check size mismatch", "Check size incompatible with fund"),
        ("E10", "Direct-only", "Direct company investing only"),
        ("E11", "Client rejected", "Rejected in prospect spreadsheet"),
        ("E12", "Prop trading", "Proprietary trading / hedge only"),
    ]
    for rule_id, name, text in exclusions:
        rules.append((rule_id, "exclusion", name, text, None, "Exclusion Filters"))

    soft_weights = [
        ("S1", "AI signal", 0.25),
        ("S2", "EM depth", 0.20),
        ("S3", "LP type priority", 0.20),
        ("S4", "Decision speed", 0.15),
        ("S5", "Stage alignment", 0.10),
        ("S6", "Clean profile", 0.05),
        ("S7", "Proxy fund overlap", 0.05),
    ]
    for rule_id, name, weight in soft_weights:
        rules.append((
            rule_id, "soft", name,
            f"Weighted soft signal (weight={weight})",
            weight, "Soft Filters",
        ))

    con.execute("DELETE FROM icp_rules")
    if rules:
        con.executemany(
            """
            INSERT INTO icp_rules (rule_id, category, rule_name, rule_text, weight, source_sheet)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rules,
        )

    return {"rows": len(rules), "icp_version": icp_spec.ICP_VERSION}
