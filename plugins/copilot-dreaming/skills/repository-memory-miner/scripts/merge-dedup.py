#!/usr/bin/env python3
"""merge-dedup.py — Merge candidate memory JSONL files from a fleet of mining
subagents into one deduplicated candidate set.

Each input line is a memory record (see the memory-mining skill schema). Inputs
come from many subagents that mined different streams, so the same fact is often
proposed more than once with slightly different wording or citations. This tool:
  - reads all input JSONL files (or a directory tree of *.jsonl, searched recursively),
  - groups records by a normalized fact key (lowercased, whitespace- and
    punctuation-collapsed),
  - within a group, keeps the "best" record (most citations, then longest reason) and
    UNIONS the citations from all members so no evidence is lost,
  - sanitizes every candidate to the author-able candidate fields — mining proposes NEW
    candidates only, so an `id` and server-managed fields (`score`,
    `votes`, timestamps, …) are dropped here, deterministically, before the existing set is
    pooled in (carry-forward and `id` preservation are owned by the later curation stage),
  - writes the merged JSONL to stdout (or --out) and a short report to stderr,
    plus a per-input WARN for any file with unparseable/factless/skipped records so a
    malformed or crashed mining subagent is visible rather than mistaken for low signal.

This is a *candidate* merge to feed the final curation pass (the skill's §9) — it
is intentionally conservative: it only collapses near-identical facts, it does not
judge importance, drop trivia, or assign rank. Do the semantic dedup/combine and
ranking in the curation step, not here.

Usage:
  merge-dedup.py <input...>            # files and/or directories
  merge-dedup.py fleet/ --out merged.jsonl
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

_norm_re = re.compile(r"[^a-z0-9]+")

# Fields a mined candidate may carry. Mining proposes NEW candidates only, so an `id`,
# a `rank` (ranking is the curator's job), and any server-managed field
# (score/votes/timestamps/...) are not author-able by a miner; we project every candidate onto
# these fields (dropping the rest) deterministically here, rather than trusting each mining
# subagent to omit them. Carry-forward, ranking, and id preservation are owned by the curator skill.
CANDIDATE_FIELDS = ("subject", "fact", "citations", "reason", "scope", "source")


def norm_fact(fact: str) -> str:
    return _norm_re.sub(" ", fact.lower()).strip()


def sanitize(rec: dict) -> tuple[dict, list[str]]:
    """Project a mined candidate onto CANDIDATE_FIELDS. Returns (clean_record, dropped_field_names)
    so a caller can report identity/server-managed fields a subagent emitted off-contract."""
    clean = {k: rec[k] for k in CANDIDATE_FIELDS if k in rec}
    dropped = [k for k in rec if k not in CANDIDATE_FIELDS]
    return clean, dropped


def iter_input_files(inputs: list[str]) -> list[str]:
    files: list[str] = []
    for inp in inputs:
        if os.path.isdir(inp):
            files.extend(sorted(glob.glob(os.path.join(inp, "**", "*.jsonl"), recursive=True)))
        else:
            files.append(inp)
    return files


def citations_of(rec: dict) -> list[str]:
    c = rec.get("citations")
    if isinstance(c, str):
        c = [c]
    elif not isinstance(c, list):
        return []
    return [x for x in c if isinstance(x, str) and x.strip()]


def better(a: dict, b: dict) -> dict:
    """Pick the record to keep as the canonical one (does not merge citations).

    Candidates are sanitized to NEW-only before this runs (no `id`), so there is no carried-vote
    signal to weigh — ties break on citation count, then reason length, then first-seen."""
    ca, cb = len(citations_of(a)), len(citations_of(b))
    if ca != cb:
        return a if ca > cb else b
    ra = len(str(a.get("reason") or ""))
    rb = len(str(b.get("reason") or ""))
    if ra != rb:
        return a if ra > rb else b
    return a


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge + dedup candidate memory JSONL.")
    ap.add_argument("inputs", nargs="+", help="JSONL files and/or directories")
    ap.add_argument("--out", default=None, help="output path (default: stdout)")
    args = ap.parse_args()

    groups: dict[str, dict] = {}  # key -> canonical record
    group_cits: dict[str, list[str]] = {}  # key -> unioned citation list (order-preserving)
    total = 0
    bad = 0
    dropped_fields: collections.Counter[str] = collections.Counter()  # forbidden field -> #records

    for path in iter_input_files(args.inputs):
        if not os.path.isfile(path):
            print(f"WARN skipping missing input: {path}", file=sys.stderr)
            continue
        file_kept = 0
        file_bad = 0
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    bad += 1
                    file_bad += 1
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("fact"), str):
                    bad += 1
                    file_bad += 1
                    continue
                key = norm_fact(rec["fact"])
                if not key:
                    bad += 1
                    file_bad += 1
                    continue
                total += 1
                file_kept += 1
                # Mining is NEW-only: drop identity/server-managed fields a subagent may have
                # carried (e.g. by self-fetching the existing export) before they enter the pool.
                rec, dropped = sanitize(rec)
                for k in dropped:
                    dropped_fields[k] += 1
                if key not in groups:
                    groups[key] = rec
                    group_cits[key] = list(dict.fromkeys(citations_of(rec)))
                else:
                    groups[key] = better(groups[key], rec)
                    for c in citations_of(rec):
                        if c not in group_cits[key]:
                            group_cits[key].append(c)
        # Surface skips per input so a stream that emitted mostly-unusable JSONL (a crashed or
        # malformed mining subagent) is visible as a failure to investigate, not silently folded
        # into the aggregate and mistaken for genuinely low signal.
        if file_bad:
            print(
                f"WARN {path}: {file_bad} of {file_bad + file_kept} record(s) "
                "unparseable/factless/skipped",
                file=sys.stderr,
            )

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        for key in sorted(groups):
            rec = dict(groups[key])
            if group_cits[key]:
                rec["citations"] = group_cits[key]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    finally:
        if args.out:
            out.close()

    print(
        f"merged {total} candidate records "
        f"({bad} unparseable/skipped) -> {len(groups)} unique facts "
        f"({total - len(groups)} duplicates collapsed)",
        file=sys.stderr,
    )
    if dropped_fields:
        summary = ", ".join(f"{k}={n}" for k, n in sorted(dropped_fields.items()))
        print(
            f"stripped non-candidate field(s) from mined records: {summary} (mining proposes NEW "
            "candidates only; identity and server-managed fields are dropped before pooling)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
