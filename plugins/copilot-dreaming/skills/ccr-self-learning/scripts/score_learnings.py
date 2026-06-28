#!/usr/bin/env python3
"""score_learnings.py — curate the agent-authored CCR learnings.

Mirrors the repository-memory miner->curator split: the agent proposes candidate learnings
from aggregates.json; this script applies deterministic thresholds, dedups, and decides how
each durable learning should be materialized (skill vs scoped memory), so the promote/hold
decision is reproducible rather than vibe-based.

Learning record schema (agent-authored learnings.json — array or {"learnings": [...]}):
  {
    "type": "suppression|convention|focus",
    "scope": "repo|org",
    "subject": "short topic",
    "statement": "what CCR should do differently, as an instruction",
    "evidence_count": 8,
    "acceptance_delta": -0.55,        # optional
    "confidence": 0.0-1.0,
    "citations": ["c001","c003", "pr#131"],
    "materialize_as": "skill|memory"  # optional hint; curator finalizes
  }

Policy (PRD): durable convention/focus with enough evidence -> generated SKILL.md; suppression
and thinner learnings -> scoped memory. Thresholds are flags, not magic — override with CLI.

Exit 0 = curated (some HELD is normal); 1 = hard validation failure; 2 = usage error.

Usage:
  score_learnings.py --learnings learnings.json [--out curated.json] [--summary summary.md]
                     [--min-evidence 4] [--min-confidence 0.6] [--skill-evidence 6]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

TYPES = {"suppression", "convention", "focus"}
SCOPES = {"repo", "org"}


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "learning"


def decide_materialization(rec: dict, skill_evidence: int) -> str:
    hint = rec.get("materialize_as")
    if hint in ("skill", "memory"):
        return hint
    if rec["type"] in ("convention", "focus") and rec.get("evidence_count", 0) >= skill_evidence:
        return "skill"
    return "memory"


def main() -> int:
    ap = argparse.ArgumentParser(description="Curate agent-authored CCR learnings.")
    ap.add_argument("--learnings", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--min-evidence", type=int, default=4)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--skill-evidence", type=int, default=6)
    args = ap.parse_args()

    try:
        with open(args.learnings, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read --learnings: {e}", file=sys.stderr)
        return 2
    learnings = data if isinstance(data, list) else data.get("learnings", [])
    if not isinstance(learnings, list) or not learnings:
        print("error: no learnings (expect array or {\"learnings\": [...]})", file=sys.stderr)
        return 1

    errors = []
    for i, r in enumerate(learnings):
        if not isinstance(r, dict):
            errors.append(f"(index {i}): not an object")
            continue
        rid = r.get("subject") or f"(index {i})"
        for key in ("type", "scope", "subject", "statement", "evidence_count", "confidence"):
            if r.get(key) in (None, ""):
                errors.append(f"{rid}: missing required field '{key}'")
        if r.get("type") and r["type"] not in TYPES:
            errors.append(f"{rid}: invalid type {r['type']!r}; expected {sorted(TYPES)}")
        if r.get("scope") and r["scope"] not in SCOPES:
            errors.append(f"{rid}: invalid scope {r['scope']!r}; expected {sorted(SCOPES)}")
        if not r.get("citations"):
            errors.append(f"{rid}: at least one citation is required")
        if isinstance(r.get("confidence"), (int, float)) and not 0 <= r["confidence"] <= 1:
            errors.append(f"{rid}: confidence must be in [0,1]")
    if errors:
        print(f"INVALID: {args.learnings}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    # Dedup by (type, scope, subject); keep highest confidence, union citations.
    merged = {}
    for r in learnings:
        key = (r["type"], r["scope"], slug(r["subject"]))
        if key not in merged:
            merged[key] = dict(r)
            merged[key]["citations"] = list(dict.fromkeys(r.get("citations", [])))
        else:
            m = merged[key]
            m["citations"] = list(dict.fromkeys(m["citations"] + r.get("citations", [])))
            m["evidence_count"] = max(m.get("evidence_count", 0), r.get("evidence_count", 0))
            if r.get("confidence", 0) > m.get("confidence", 0):
                m["confidence"] = r["confidence"]
                m["statement"] = r["statement"]

    curated, held = [], []
    for key, r in merged.items():
        reason = []
        if r.get("evidence_count", 0) < args.min_evidence:
            reason.append(f"evidence {r.get('evidence_count',0)} < {args.min_evidence}")
        if r.get("confidence", 0) < args.min_confidence:
            reason.append(f"confidence {r.get('confidence',0)} < {args.min_confidence}")
        if reason:
            r["held_reason"] = "; ".join(reason)
            held.append(r)
            continue
        r["materialize_as"] = decide_materialization(r, args.skill_evidence)
        if r["materialize_as"] == "skill":
            r["proposed_skill_dir"] = f"ccr-{r['type']}-{slug(r['subject'])}"
        curated.append(r)

    curated.sort(key=lambda r: (r["materialize_as"], -r.get("confidence", 0)))
    n_skill = sum(1 for r in curated if r["materialize_as"] == "skill")
    n_mem = sum(1 for r in curated if r["materialize_as"] == "memory")
    print(f"CURATED: {len(curated)} promoted ({n_skill} skill, {n_mem} memory), {len(held)} held")

    result = {
        "curated": curated, "held": held,
        "counts": {"promoted": len(curated), "skill": n_skill, "memory": n_mem, "held": len(held)},
        "thresholds": {"min_evidence": args.min_evidence, "min_confidence": args.min_confidence,
                       "skill_evidence": args.skill_evidence},
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
        print(f"wrote {args.out}", file=sys.stderr)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(_render_summary(result))
        print(f"wrote {args.summary}", file=sys.stderr)
    return 0


def _render_summary(result: dict) -> str:
    lines = ["# CCR self-learning — proposed learnings (shadow mode)\n",
             f"Promoted **{result['counts']['promoted']}** "
             f"({result['counts']['skill']} skill, {result['counts']['memory']} memory); "
             f"held {result['counts']['held']}.\n",
             "| Type | Scope | Materialize | Conf | Evidence | Subject | Statement |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in result["curated"]:
        lines.append(
            f"| {r['type']} | {r['scope']} | {r['materialize_as']} | {r.get('confidence','')} | "
            f"{r.get('evidence_count','')} | {r['subject']} | "
            f"{r['statement'].replace(chr(10),' ')} |")
    if result["held"]:
        lines.append("\n## Held (below threshold)\n")
        for r in result["held"]:
            lines.append(f"- **{r['subject']}** ({r['type']}): {r.get('held_reason','')}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
