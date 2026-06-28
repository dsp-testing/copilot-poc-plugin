#!/usr/bin/env python3
"""aggregate_outcomes.py — deterministically aggregate the CCR outcome-labeled corpus
into the signals the agent reasons over to propose typed learnings.

Reads a JSONL corpus in the shape of assets/corpus.schema.json (the connector contract).
Computes, deterministically (so two model runs can't disagree on the numbers):

  totals          : overall accepted/rejected/engaged + baseline acceptance_rate
  by_category      : per (repo, category) acceptance/rejection rate + delta vs baseline
                     -> high rejection + volume => SUPPRESSION candidate
                     -> high acceptance + volume => FOCUS candidate
  by_path_prefix   : where REJECTED ccr comments cluster (top path segments)
                     -> SUPPRESSION-on-path candidate (generated/, db/migrate/, lockfiles/)
  human_conventions: clustered human_followup / human-authored signals
                     -> CONVENTION candidate (what humans enforce that CCR missed)

The agent turns these aggregates into learnings.json; score_learnings.py then thresholds,
dedups, and decides skill-vs-memory. This script does NO thresholding — it only counts.

Acceptance math: denom = accepted + rejected + ignored (engaged/superseded excluded as
neither clear accept nor reject). acceptance_rate = accepted / denom (None if denom 0).

Usage:
  aggregate_outcomes.py --corpus corpus.jsonl [--out aggregates.json] [--path-depth 2]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

ACCEPT = {"accepted"}
REJECT = {"rejected", "ignored"}
NEUTRAL = {"engaged", "superseded"}


def _rate(accepted, rejected):
    denom = accepted + rejected
    return round(accepted / denom, 3) if denom else None


def path_prefix(path: str, depth: int) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return "/".join(parts[:depth])


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate the CCR outcome-labeled corpus.")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", default="-")
    ap.add_argument("--path-depth", type=int, default=2)
    args = ap.parse_args()

    records = []
    try:
        with open(args.corpus, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"warn: skipping unparseable line {ln}: {e}", file=sys.stderr)
    except OSError as e:
        print(f"error: cannot read --corpus: {e}", file=sys.stderr)
        return 2
    if not records:
        print("error: empty corpus", file=sys.stderr)
        return 1

    ccr = [r for r in records if r.get("source") == "ccr"]

    # --- totals (baseline from CCR comments only) ---
    t_acc = sum(1 for r in ccr if r.get("outcome") in ACCEPT)
    t_rej = sum(1 for r in ccr if r.get("outcome") in REJECT)
    t_eng = sum(1 for r in ccr if r.get("outcome") in NEUTRAL)
    baseline = _rate(t_acc, t_rej)

    # --- by (repo, category) over CCR comments ---
    cat = defaultdict(lambda: {"n": 0, "accepted": 0, "rejected": 0, "engaged": 0,
                               "thumbs_down": 0, "examples": []})
    for r in ccr:
        key = (r.get("repo", ""), r.get("category", "uncategorized"))
        c = cat[key]
        c["n"] += 1
        oc = r.get("outcome")
        if oc in ACCEPT:
            c["accepted"] += 1
        elif oc in REJECT:
            c["rejected"] += 1
        else:
            c["engaged"] += 1
        c["thumbs_down"] += (r.get("reactions") or {}).get("thumbs_down", 0)
        if len(c["examples"]) < 3 and r.get("comment_id"):
            c["examples"].append(r["comment_id"])

    by_category = []
    for (repo, category), c in sorted(cat.items()):
        ar = _rate(c["accepted"], c["rejected"])
        by_category.append({
            "repo": repo, "category": category, "n": c["n"],
            "accepted": c["accepted"], "rejected": c["rejected"], "engaged": c["engaged"],
            "thumbs_down": c["thumbs_down"],
            "acceptance_rate": ar,
            "acceptance_delta": (round(ar - baseline, 3) if ar is not None and baseline is not None else None),
            "examples": c["examples"],
            "signal": _category_signal(c, ar, baseline),
        })

    # --- by path prefix over REJECTED ccr comments (noise clusters) ---
    prefix = defaultdict(lambda: {"rejected": 0, "total": 0, "categories": set(), "examples": []})
    for r in ccr:
        pp = path_prefix(r.get("path", ""), args.path_depth)
        if not pp:
            continue
        p = prefix[pp]
        p["total"] += 1
        if r.get("outcome") in REJECT:
            p["rejected"] += 1
            p["categories"].add(r.get("category", ""))
            if len(p["examples"]) < 3 and r.get("comment_id"):
                p["examples"].append(r["comment_id"])
    by_path_prefix = sorted(
        ({"path_prefix": k, "rejected": v["rejected"], "total": v["total"],
          "categories": sorted(c for c in v["categories"] if c), "examples": v["examples"]}
         for k, v in prefix.items() if v["rejected"] >= 2),
        key=lambda d: d["rejected"], reverse=True,
    )

    # --- human-enforced conventions ---
    conv = defaultdict(lambda: {"n": 0, "paths": set(), "examples": [], "categories": set()})
    for r in records:
        fu = r.get("human_followup")
        is_human = r.get("source") == "human"
        if not fu and not is_human:
            continue
        key = (fu or r.get("body", "")[:60]).strip().lower()
        cc = conv[key]
        cc["n"] += 1
        if r.get("path"):
            cc["paths"].add(path_prefix(r["path"], args.path_depth))
        cc["categories"].add(r.get("category", ""))
        if len(cc["examples"]) < 4 and r.get("comment_id"):
            cc["examples"].append(r["comment_id"])
    human_conventions = sorted(
        ({"convention": k, "occurrences": v["n"],
          "paths": sorted(p for p in v["paths"] if p),
          "categories": sorted(c for c in v["categories"] if c),
          "examples": v["examples"]}
         for k, v in conv.items() if v["n"] >= 2),
        key=lambda d: d["occurrences"], reverse=True,
    )

    out = {
        "corpus_size": len(records),
        "ccr_comments": len(ccr),
        "totals": {"accepted": t_acc, "rejected": t_rej, "engaged": t_eng,
                   "baseline_acceptance_rate": baseline},
        "by_category": by_category,
        "by_path_prefix": by_path_prefix,
        "human_conventions": human_conventions,
        "hint": ("Propose learnings from these aggregates: by_category.signal=='suppression' or "
                 "by_path_prefix -> suppression; by_category.signal=='focus' -> focus; "
                 "human_conventions -> convention. score_learnings.py applies the thresholds."),
    }
    payload = json.dumps(out, indent=2)
    if args.out == "-":
        print(payload)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


def _category_signal(c, ar, baseline):
    """Directional hint only (not a threshold): suppression / focus / neutral."""
    if c["n"] < 3 or ar is None:
        return "neutral"
    if ar <= 0.34 or (baseline is not None and ar <= baseline - 0.3):
        return "suppression"
    if ar >= 0.8 and (baseline is None or ar >= baseline + 0.2):
        return "focus"
    return "neutral"


if __name__ == "__main__":
    raise SystemExit(main())
