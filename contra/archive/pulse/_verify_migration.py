import uuid
from agents.db import get_conn

con = get_conn()
print("Migration ran OK")

rid = str(uuid.uuid4())
con.execute(
    "INSERT INTO pipeline_runs (run_id, stage, status, started_at) VALUES (?, 'research', 'running', NOW())",
    [rid],
)
con.execute("DELETE FROM pipeline_runs WHERE run_id = ?", [rid])
print("research stage: ACCEPTED")
print("All good — run `python -m pulse research enrich --only-unknown --limit 5`")
