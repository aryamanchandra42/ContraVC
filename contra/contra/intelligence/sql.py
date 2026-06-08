"""Read-only SQL execution with whitelist validation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_ALLOWED = frozenset({
    "allocators", "allocators_effective",
    "icp_scores", "icp_rules", "crm_contacts",
    "relationships", "relationships_effective",
    "signals", "funds", "investments", "rejections",
    "benchmark_rankings", "entity_aliases",
    "v_lp_profile", "v_crm_contacts", "v_document_chunks",
    "entities_raw",
})

_DDL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|EXEC|EXECUTE|PRAGMA)\b",
    re.IGNORECASE,
)
_MAX_LIMIT = 50


def validate_select(sql: str) -> Tuple[str, Optional[str]]:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return "", "Multiple statements not allowed."
    if not stripped.upper().lstrip().startswith("SELECT"):
        return "", "Only SELECT allowed."
    if _DDL.search(stripped):
        return "", f"Disallowed keyword: {_DDL.search(stripped).group()}."
    if not re.search(r"\bLIMIT\s+\d+\b", stripped, re.IGNORECASE):
        stripped = f"{stripped}\nLIMIT {_MAX_LIMIT}"
    return stripped, None


def run_readonly_sql(con, sql: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cleaned, err = validate_select(sql)
    if err:
        return [], err
    try:
        df = con.execute(cleaned).fetchdf()
        return df.to_dict(orient="records"), None
    except Exception as exc:
        return [], str(exc)


def execute_template(con, template_sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    cleaned, err = validate_select(template_sql)
    if err:
        raise ValueError(err)
    df = con.execute(cleaned, params).fetchdf()
    return df.to_dict(orient="records")
