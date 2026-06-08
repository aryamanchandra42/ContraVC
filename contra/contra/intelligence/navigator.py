"""Navigator Protocol — deterministic drill-down before LLM verdict."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal

import yaml

from contra.intelligence.brief import IntelligenceBrief
from contra.intelligence.resolver import norm_key
from contra.intelligence.sql import execute_template

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_PATH = ROOT / "prompts" / "navigator" / "query_templates.yaml"

Coverage = Literal["rich", "partial", "thin", "none"]


def _load_templates() -> Dict[str, str]:
    if not TEMPLATES_PATH.exists():
        return {}
    data = yaml.safe_load(TEMPLATES_PATH.read_text(encoding="utf-8")) or {}
    return {k: v["sql"] for k, v in (data.get("templates") or {}).items() if "sql" in v}


def assess_coverage(brief: IntelligenceBrief) -> Coverage:
    if brief.match_confidence >= 0.92 and brief.icp_tier:
        return "rich"
    if brief.allocator_id and brief.match_confidence >= 0.85:
        return "partial"
    if brief.match_confidence >= 0.70:
        return "thin"
    return "none"


def plan_drill_down(brief: IntelligenceBrief, coverage: Coverage) -> List[Dict[str, Any]]:
    name = brief.input_name
    plans: List[Dict[str, Any]] = []
    if coverage == "none":
        plans = [
            {"template_id": "fuzzy_alias_lookup", "params": [name, name]},
            {"template_id": "document_search", "params": [name]},
        ]
    elif coverage == "thin":
        plans = [
            {"template_id": "lp_profile", "params": [name, norm_key(name)]},
            {"template_id": "document_search", "params": [name]},
        ]
    elif coverage == "partial" and brief.allocator_id:
        plans = [{"template_id": "lp_signals", "params": [brief.allocator_id]}]
        warm = (brief.graph_connectivity or {}).get("warm_path_count", 0)
        if not warm:
            plans.append({
                "template_id": "lp_relationships",
                "params": [brief.allocator_id, brief.allocator_id],
            })
    return plans[:2]


def run_drill_down(con, brief: IntelligenceBrief) -> List[Dict[str, Any]]:
    templates = _load_templates()
    coverage = assess_coverage(brief)
    results: List[Dict[str, Any]] = []
    for plan in plan_drill_down(brief, coverage):
        tid = plan["template_id"]
        sql = templates.get(tid)
        if not sql:
            continue
        try:
            rows = execute_template(con, sql, plan["params"])
            results.append({"template_id": tid, "row_count": len(rows), "rows": rows[:20]})
        except Exception as exc:
            results.append({"template_id": tid, "error": str(exc), "rows": []})
    return results
