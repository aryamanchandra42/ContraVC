"""
Paired A/B backtest of the gate against the client's own decisions.

WHY THIS EXISTS
---------------
`gate_explain.yaml` and `appetite_validator.py` were changed to stop the gate
rejecting fund-of-funds as "GPs, not LPs". That prompt is shared with the manual LP
Gate screening path, so the change can alter verdicts on entities that have nothing
to do with the prospector. Both edits LOOSEN rejection, which is the dangerous
direction: the failure mode is new false positives in manual screening, and nothing
in the test suite would notice.

`eval_prerank.py` covers Stage 3 of the prospector only. Nothing measured the gate.

METHOD
------
Paired A/B on identical inputs, both arms run back to back against the same
research cache so the comparison is not confounded by the web changing underneath:

  before  = prompt at git HEAD      + fund-of-funds backstop disabled
  after   = prompt in working tree  + fund-of-funds backstop active

The "before" arm is reconstructed rather than remembered, because no raw gate
explanation is stored anywhere: the batch checkpoints keep only final verdicts, and
`crm_gate_reviews` and `lead_scorecards` were emptied by the CRM reset. Reconstruction
is also the stronger design — both arms run today, against the same models and the
same cached evidence, so a difference between them is attributable to the diff.

Ground truth is `icp_scores.client_decision`. The sample is weighted toward
client-REJECTED entities, since that is where a loosened NO shows up.

READING THE OUTPUT
------------------
The headline number is HARMFUL FLIPS: entities the client rejected that the gate
used to reject and now does not.

Interpret it against the NOISE FLOOR, printed alongside it. The verdict models are
not deterministic, so re-running the identical arm twice produces some
disagreement on its own. A harmful-flip count at or below that floor is
indistinguishable from model variance and should not be read as a regression.
Without that baseline a raw flip count means nothing, which is why --noise-control
runs by default.

The gate WRITES BACK on yes/review verdicts (allocators, dossiers, contacts), so
this runs against a throwaway copy of the database AND no-ops the persistence
functions. Otherwise the first arm would change what the second arm looks up.

USAGE
    python scripts/backtest_gate.py                     # 12 rejected + 8 approved
    python scripts/backtest_gate.py --rejected 30 --approved 20
    python scripts/backtest_gate.py --noise-control 0   # skip the noise baseline
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Repo root, one level above the `contra` package dir, for `git show`.
GIT_ROOT = ROOT.parent
PROMPT_REL = "contra/prompts/navigator/gate_explain.yaml"

_print_lock = threading.Lock()


def _say(msg: str = "") -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Arm setup — the two things the diff touched
# ---------------------------------------------------------------------------

def _prompt_at_head() -> Optional[Dict[str, Any]]:
    """Parse the committed gate_explain.yaml, or None if git cannot supply it."""
    import yaml

    try:
        raw = subprocess.run(
            ["git", "show", f"HEAD:{PROMPT_REL}"],
            cwd=GIT_ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        _say(f"  ! could not read {PROMPT_REL} at HEAD: {exc}")
        return None
    try:
        return yaml.safe_load(raw) or {}
    except Exception as exc:
        _say(f"  ! could not parse HEAD prompt: {exc}")
        return None


class Arm:
    """
    Installs one side of the A/B, then restores what it changed.

    Both knobs are module attributes read at call time, so patching them is enough
    and no production code needs a test-only branch:
      - verdict._load_yaml reads the prompt fresh on every explain call
      - appetite_validator._LP_BY_CONSTRUCTION gates the fund-of-funds backstop,
        so emptying it reproduces the previous behaviour exactly
    """

    def __init__(self, name: str, head_prompt: Optional[Dict[str, Any]]):
        self.name = name
        self.head_prompt = head_prompt
        self._undo: List[Tuple[Any, str, Any]] = []

    def _patch(self, module: Any, attr: str, value: Any) -> None:
        self._undo.append((module, attr, getattr(module, attr)))
        setattr(module, attr, value)

    def __enter__(self) -> "Arm":
        from contra.gate import appetite_validator, persist, verdict
        from contra.crm import dossier

        # Never write during a backtest, in either arm.
        self._patch(persist, "persist_gate_findings", lambda *a, **k: None)
        self._patch(dossier, "upsert_dossier_from_gate", lambda *a, **k: None)

        if self.name == "before":
            self._patch(appetite_validator, "_LP_BY_CONSTRUCTION", frozenset())
            if self.head_prompt:
                original = verdict._load_yaml

                def _old_prompt(fname: str, _orig=original, _p=self.head_prompt):
                    return _p if fname == "gate_explain.yaml" else _orig(fname)

                self._patch(verdict, "_load_yaml", _old_prompt)
        return self

    def __exit__(self, *exc) -> None:
        for module, attr, old in reversed(self._undo):
            setattr(module, attr, old)
        self._undo.clear()


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------

def pick_sample(con, n_rejected: int, n_approved: int, seed: int) -> List[Dict[str, Any]]:
    """Labeled allocators, weighted toward client-REJECTED entities."""
    rows: List[Dict[str, Any]] = []
    for label, want in (("rejected", n_rejected), ("approved", n_approved)):
        if want <= 0:
            continue
        found = con.execute(
            """
            SELECT a.canonical_name, s.stated_reason
            FROM icp_scores s
            JOIN allocators a ON a.allocator_id = s.allocator_id
            WHERE s.client_decision = ?
              AND COALESCE(a.canonical_name, '') <> ''
            """,
            [label],
        ).fetchall()
        random.Random(seed).shuffle(found)
        seen: set = set()
        for name, reason in found:
            key = (name or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append({"name": name, "label": label, "reason": reason or ""})
            if len([r for r in rows if r["label"] == label]) >= want:
                break
    return rows


# ---------------------------------------------------------------------------
# One gate run
# ---------------------------------------------------------------------------

def _verdict_of(result: Any) -> str:
    if getattr(result, "yes", False):
        return "yes"
    return "review" if getattr(result, "is_review", False) else "no"


def _backstop_fired(result: Any) -> bool:
    """True when the fund-of-funds contradiction rule moved this verdict."""
    for c in (getattr(result, "conflicts", None) or []):
        low = str(c).lower()
        if "contradicts" in low and "archetype" in low:
            return True
    return False


def run_arm(db_path: Path, arm: str, sample: List[Dict[str, Any]],
            head_prompt: Optional[Dict[str, Any]], workers: int) -> Dict[str, Dict[str, Any]]:
    """Screen every sampled entity under one arm. Returns {name: outcome}."""
    from agents.db import get_conn
    from contra.gate.runner import run_gate

    out: Dict[str, Dict[str, Any]] = {}
    done = [0]

    def _one(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        # A connection per task, because DuckDB connections are not thread-safe, and
        # read_only because that is the only mode that skips `_bootstrap`. Two
        # concurrent writable connections both try to create the schema indexes and
        # collide: "write-write conflict on ... idx_signals_allocator". The gate needs
        # no writes here anyway — Arm has already no-op'd both persistence calls.
        con = get_conn(db_path, read_only=True)
        try:
            result = run_gate(con, row["name"], screening_mode="institutional")
            rec = {
                "verdict": _verdict_of(result),
                "summary": (getattr(result, "summary", "") or "")[:300],
                "backstop": _backstop_fired(result),
                "error": "",
            }
        except Exception as exc:
            rec = {"verdict": "error", "summary": "", "backstop": False,
                   "error": f"{type(exc).__name__}: {exc}"[:200]}
        finally:
            try:
                con.close()
            except Exception:
                pass
        done[0] += 1
        _say(f"  [{arm}] {done[0]:>3}/{len(sample)}  {row['name'][:38]:40s} -> {rec['verdict']}")
        return row["name"], rec

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, r) for r in sample]
        for fut in as_completed(futures):
            try:
                name, rec = fut.result()
                out[name] = rec
            except Exception as exc:
                _say(f"  ! arm task failed: {exc}")
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _agrees(label: str, verdict: str) -> Optional[bool]:
    """
    Does a gate verdict agree with the client's decision?

    REVIEW is deliberately counted as agreement with 'approved' and disagreement
    with 'rejected': REVIEW means "a human should look", which is right for a
    candidate the client took and wrong for one they threw out.
    """
    if verdict == "error" or label not in ("rejected", "approved"):
        return None
    if label == "rejected":
        return verdict == "no"
    return verdict in ("yes", "review")


def report(sample: List[Dict[str, Any]], before: Dict[str, Dict[str, Any]],
           after: Dict[str, Dict[str, Any]], noise: Dict[str, str]) -> int:
    by_name = {r["name"]: r for r in sample}
    paired = [
        (n, by_name[n], before[n], after[n])
        for n in by_name
        if n in before and n in after
        and before[n]["verdict"] != "error" and after[n]["verdict"] != "error"
    ]

    _say("")
    _say("=" * 78)
    _say("GATE BACKTEST — before (HEAD) vs after (working tree)")
    _say("=" * 78)

    errs = [n for n in by_name if (before.get(n, {}).get("verdict") == "error"
                                   or after.get(n, {}).get("verdict") == "error")]
    _say(f"  screened in both arms : {len(paired)}")
    if errs:
        _say(f"  dropped (error)       : {len(errs)}  {', '.join(e[:22] for e in errs[:5])}")
    if not paired:
        _say("\n  NOTHING COMPARABLE — cannot conclude anything.")
        return 1

    # ----- agreement with the client, per arm -----
    _say("")
    _say("AGREEMENT WITH CLIENT DECISION")
    _say(f"  {'label':10s} {'n':>4s}   {'before':>8s}   {'after':>8s}")
    for label in ("rejected", "approved"):
        rows = [p for p in paired if p[1]["label"] == label]
        if not rows:
            continue
        b = sum(1 for _, m, bef, _ in rows if _agrees(label, bef["verdict"]))
        a = sum(1 for _, m, _, aft in rows if _agrees(label, aft["verdict"]))
        _say(f"  {label:10s} {len(rows):>4d}   {b:>3d}/{len(rows):<4d}  {a:>3d}/{len(rows):<4d}")

    # ----- verdict distribution shift -----
    cb = Counter(bef["verdict"] for _, _, bef, _ in paired)
    ca = Counter(aft["verdict"] for _, _, _, aft in paired)
    _say("")
    _say("VERDICT MIX")
    _say(f"  {'verdict':8s} {'before':>7s} {'after':>7s}")
    for v in ("yes", "review", "no"):
        _say(f"  {v:8s} {cb.get(v,0):>7d} {ca.get(v,0):>7d}")

    # ----- flips -----
    flips = [p for p in paired if p[2]["verdict"] != p[3]["verdict"]]
    harmful = [p for p in flips if p[1]["label"] == "rejected"
               and p[2]["verdict"] == "no" and p[3]["verdict"] != "no"]
    helpful = [p for p in flips if p[1]["label"] == "approved"
               and p[2]["verdict"] == "no" and p[3]["verdict"] != "no"]

    _say("")
    _say(f"FLIPS: {len(flips)} of {len(paired)}")
    if flips:
        _say(f"  {'name':32s} {'client':9s} {'before':7s} {'after':7s} cause")
        for name, meta, bef, aft in sorted(flips, key=lambda p: p[1]["label"]):
            cause = "FoF backstop" if aft["backstop"] else "prompt"
            _say(f"  {name[:30]:32s} {meta['label']:9s} {bef['verdict']:7s} "
                 f"{aft['verdict']:7s} {cause}")

    # ----- the headline, against the noise floor -----
    _say("")
    _say("-" * 78)
    if noise:
        disagreed = sum(1 for v in noise.values() if v == "differs")
        _say(f"NOISE FLOOR   : {disagreed} of {len(noise)} entities changed verdict when the "
             f"IDENTICAL arm ran twice")
    else:
        _say("NOISE FLOOR   : not measured (--noise-control 0) — flips below are "
             "NOT distinguishable from model variance")
    _say(f"HARMFUL FLIPS : {len(harmful)}  (client rejected, gate no -> not-no)")
    _say(f"HELPFUL FLIPS : {len(helpful)}  (client approved, gate no -> not-no)")
    for name, meta, _, aft in harmful:
        _say(f"    - {name[:40]:42s} reason: {str(meta['reason'])[:44]}")
        _say(f"      now: {aft['summary'][:150]}")

    noise_floor = sum(1 for v in noise.values() if v == "differs") if noise else None
    _say("")
    if not harmful:
        _say("VERDICT  no harmful flip observed — the change did not loosen any")
        _say("         rejection the client agreed with, in this sample.")
    elif noise_floor is not None and len(harmful) <= noise_floor:
        _say("VERDICT  harmful flips are AT OR BELOW the noise floor — consistent with")
        _say("         model variance rather than the diff. Re-run with a larger sample")
        _say("         before concluding either way.")
    else:
        _say("VERDICT  harmful flips EXCEED the noise floor. The change is loosening")
        _say("         rejections the client agreed with. Review the cases above.")
    _say("-" * 78)
    return 0


# ---------------------------------------------------------------------------
# Noise control
# ---------------------------------------------------------------------------

def measure_noise(db_path: Path, sample: List[Dict[str, Any]], n: int,
                  head_prompt: Optional[Dict[str, Any]], workers: int,
                  first_pass: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """
    Re-run the SAME ('after') arm on n entities to size model non-determinism.

    Any flip count is meaningless without this: the verdict models are sampled, not
    deterministic, so some disagreement appears with no code change at all.
    """
    subset = [r for r in sample if first_pass.get(r["name"], {}).get("verdict") not in (None, "error")][:n]
    if not subset:
        return {}
    _say("")
    _say(f"--- noise control: re-running the 'after' arm on {len(subset)} entities ---")
    with Arm("after", head_prompt):
        repeat = run_arm(db_path, "noise", subset, head_prompt, workers)
    out: Dict[str, str] = {}
    for row in subset:
        name = row["name"]
        a = first_pass.get(name, {}).get("verdict")
        b = repeat.get(name, {}).get("verdict")
        if a and b and b != "error":
            out[name] = "same" if a == b else "differs"
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rejected", type=int, default=12,
                    help="client-rejected entities to sample (default 12)")
    ap.add_argument("--approved", type=int, default=8,
                    help="client-approved entities to sample (default 8)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--noise-control", type=int, default=6,
                    help="entities to re-screen for a variance baseline (0 to skip)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--names", default="",
                    help="comma-separated entities to screen instead of sampling; "
                         "use to A/B specific cases the labeled sample does not contain")
    ap.add_argument("--out", default="", help="write per-entity results as JSONL")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    # `get_conn` IGNORES its db_path argument when MOTHERDUCK_TOKEN is set and
    # connects to md:contra instead, so in cloud mode the throwaway copy below would
    # be silently bypassed and this would screen against production. Drop the token
    # for this process rather than risk that.
    if os.environ.pop("MOTHERDUCK_TOKEN", "").strip():
        _say("MOTHERDUCK_TOKEN ignored for this run — a backtest must not touch the")
        _say("cloud database. Using the local file below instead, which may be staler.")

    from agents.db import DB_PATH, get_conn

    src = Path(DB_PATH)
    if not src.exists():
        _say(f"database not found: {src}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="gate-backtest-"))
    db_path = tmp / "backtest.duckdb"
    shutil.copy2(src, db_path)
    _say(f"Working against a copy: {db_path}")

    con = get_conn(db_path)
    if args.names.strip():
        sample = [{"name": n.strip(), "label": "unlabeled", "reason": ""}
                  for n in args.names.split(",") if n.strip()]
    else:
        sample = pick_sample(con, args.rejected, args.approved, args.seed)
    con.close()
    if not sample:
        _say("No entities to screen — nothing to back-test.")
        return 1

    n_rej = sum(1 for r in sample if r["label"] == "rejected")
    if args.names.strip():
        _say(f"Explicit list: {len(sample)} entities (no client label — flips only)")
    else:
        _say(f"Sample: {len(sample)} entities ({n_rej} client-rejected, "
             f"{len(sample) - n_rej} client-approved)")

    head_prompt = _prompt_at_head()
    if head_prompt is None:
        _say("  ! HEAD prompt unavailable — the 'before' arm will differ only by the")
        _say("    validator backstop, so a prompt regression would go unseen.")

    _say("")
    _say("--- arm: before (HEAD prompt, backstop off) ---")
    with Arm("before", head_prompt):
        before = run_arm(db_path, "before", sample, head_prompt, args.workers)

    _say("")
    _say("--- arm: after (working tree) ---")
    with Arm("after", head_prompt):
        after = run_arm(db_path, "after", sample, head_prompt, args.workers)

    noise = measure_noise(db_path, sample, args.noise_control, head_prompt,
                          args.workers, after) if args.noise_control > 0 else {}

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for row in sample:
                n = row["name"]
                fh.write(json.dumps({
                    "name": n, "label": row["label"], "reason": row["reason"],
                    "before": before.get(n), "after": after.get(n),
                    "noise": noise.get(n),
                }) + "\n")
        _say(f"\nPer-entity results: {args.out}")

    code = report(sample, before, after, noise)
    shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
