"""
PULSE CLI entrypoint.

Usage:
    python -m pulse <command>
    pulse <command>  (if installed via pip install -e .)

Commands:
    ingest      Ingest all source files → entities_raw
    normalize   Entity resolution + taxonomy mapping → canonical tables
    extract     Ontology extraction pipeline → ontology_terms + relationship_evidence
    derive      Recompute uncertainty + temporal derivations
    review      Review queue management (list / ingest / status)
    graph       Build and persist relationship graph
    run-all     Full pipeline: ingest → normalize → extract → derive → graph
    status      Last run summary per stage
    research    Research agent: enrich / ask / brief / ontology
"""

from __future__ import annotations

from pathlib import Path

# Auto-load .env from the repo root if python-dotenv is installed.
# This means PULSE_LLM_PROVIDER, GROQ_API_KEY, TAVILY_API_KEY etc. are
# available to every subcommand without manually exporting them each session.
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass  # dotenv not installed — env vars must be set manually

from pulse.cli import app

if __name__ == "__main__":
    app()
