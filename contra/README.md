# Contra — LP intelligence backend + CRM gate

## Setup

```bash
cd contra
pip install -e ".[gate]"
```

Required for `contra gate` on **new** LPs (not already in CRM):

1. Copy `env.example` to `.env` and add your keys
2. Or set env vars manually in the shell

```bash
cp env.example .env   # then edit .env
pip install -e ".[gate]"
```

**Recommended (free tiers):** Groq + Tavily — see `env.example` for variable names.

For LP Gate, use `PULSE_LLM_PROVIDER=groq` and `PULSE_LLM_MODEL=llama-3.3-70b-versatile`
(the 8b model often hits Groq free-tier context limits on gate prompts).

## Commands

```bash
contra refresh    # rebuild contra.duckdb from raw_data/
contra catalog    # data estate summary
contra gate "LP Name"   # YES/NO CRM decision
contra gate "LP Name" --json
```

## Data

All source files live in `raw_data/`. The proprietary database is `contra.duckdb`.
