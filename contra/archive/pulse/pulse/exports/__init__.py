"""
pulse.exports — library functions for outreach CSV generation.

These replace the standalone scripts/ and are called by the orchestrator
and the Streamlit UI. They accept a live DuckDB connection so they never
open their own DB handle.
"""
