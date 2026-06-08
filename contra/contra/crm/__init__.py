"""CRM leads — write path, scoring, and promotion from gate/prospects."""

from contra.crm.writer import add_lead_from_gate, promote_prospect, upsert_manual_lead

__all__ = ["add_lead_from_gate", "promote_prospect", "upsert_manual_lead"]
