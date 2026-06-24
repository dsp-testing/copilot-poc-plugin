#!/usr/bin/env python3
"""validate-output.py — Validate a curated memory-mining JSONL before it is treated
as the full-replacement memory set for a repo.

Checks every record for the fields the memory store requires and that the
memory-mining skill mandates:
  - valid JSON object, one per line
  - non-empty `subject`, `fact`, `reason`
  - `citations` is a non-empty list of non-empty strings
  - `scope` == "repository" (user-scoped memories must never appear)
  - `source` is an object with non-empty `interactionId` and `agent` (assemble-final.py stamps
    this authoritatively)
  - `rank` is required and a positive integer; across the file the ranks must be
    unique (the final deliverable is a unique total ordering, no ties)
  - no record carries a pipeline-internal tag (`_prov`/`_bucket`)
Also warns (does not fail) on likely-duplicate `fact` strings, on ranks that are unique
but not a contiguous 1..N sequence, and on records whose cited paths look like local files but
cannot be found on disk (best-effort; only checked when --repo-root is given).

Because the output is treated as the **full-replacement** memory set, an empty file is
rejected as a hard error (it would delete every existing memory by omission).

Exit code 0 = all records valid; 1 = at least one hard error; 2 = usage error.
Run from anywhere; pass --repo-root <checkout> to enable on-disk citation checks.
Pass --pre-assembly to validate a single pre-assembly bucket slice: it skips the `source`
requirement and rank uniqueness/contiguity (a slice has no stamped source and carries coarse,
intentionally-tied tier ranks) and treats an empty slice as a warning; all other checks hold.

Usage:
  validate-output.py <curated.jsonl> [--repo-root <dir>] [--strict-citations] [--pre-assembly]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from citation_paths import citation_file_paths

REQUIRED_STR = ("subject", "fact", "reason")

# Pipeline-internal tags (added by bucket-pool.py) that must NEVER appear in a deliverable record,
# carried-forward or not.
INTERIM_FIELDS = ("_prov", "_bucket")

def is_nonempty_str(v: object) -> bool:
    return isinstance(v, str) and v.strip() != ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate curated memory JSONL.")
    ap.add_argument("jsonl")
    ap.add_argument(
        "--repo-root",
        default=None,
        help="repo checkout; enables best-effort on-disk citation checks",
    )
    ap.add_argument(
        "--strict-citations",
        action="store_true",
        help="treat missing cited files as errors, not warnings",
    )
    ap.add_argument(
        "--pre-assembly",
        action="store_true",
        help="validate a pre-assembly bucket slice: skip the `source` requirement and rank "
        "uniqueness/contiguity (a slice has no stamped source and carries coarse tied tier ranks), "
        "and treat an empty slice as a warning; all other checks are unchanged",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.jsonl):
        print(f"error: file not found: {args.jsonl}", file=sys.stderr)
        return 2

    if args.repo_root is not None and not os.path.isdir(args.repo_root):
        print(f"error: --repo-root is not a directory: {args.repo_root}", file=sys.stderr)
        return 2

    errors: list[str] = []
    warnings: list[str] = []
    facts: Counter[str] = Counter()
    ranks: list[int] = []
    n = 0

    with open(args.jsonl, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: invalid JSON: {e}")
                continue
            if not isinstance(rec, dict):
                errors.append(f"line {lineno}: not a JSON object")
                continue
            n += 1

            for k in REQUIRED_STR:
                if not is_nonempty_str(rec.get(k)):
                    errors.append(f"line {lineno}: missing/empty `{k}`")

            present_interim = [k for k in INTERIM_FIELDS if k in rec]
            if present_interim:
                errors.append(
                    f"line {lineno}: must not carry pipeline-internal field(s) "
                    f"{', '.join(present_interim)} (these are bucketing tags, never part of a "
                    f"deliverable record)"
                )

            cits = rec.get("citations")
            if not isinstance(cits, list) or not cits or not all(is_nonempty_str(c) for c in cits):
                errors.append(
                    f"line {lineno}: `citations` must be a non-empty list of non-empty strings"
                )
                cits = []

            if rec.get("scope") != "repository":
                errors.append(
                    f'line {lineno}: `scope` must be "repository" (got {rec.get("scope")!r})'
                )

            if not args.pre_assembly:
                src = rec.get("source")
                if not isinstance(src, dict):
                    errors.append(f"line {lineno}: `source` must be an object")
                else:
                    if not is_nonempty_str(src.get("interactionId")):
                        errors.append(
                            f"line {lineno}: `source.interactionId` must be a non-empty string"
                        )
                    if not is_nonempty_str(src.get("agent")):
                        errors.append(f"line {lineno}: `source.agent` must be a non-empty string")

            rank = rec.get("rank")
            valid_rank = isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1
            if args.pre_assembly:
                # A slice carries coarse, intentionally-tied tier ranks (or none yet): only flag a
                # present-but-invalid rank, and never feed the uniqueness/contiguity checks below.
                if rank is not None and not valid_rank:
                    errors.append(
                        f"line {lineno}: `rank`, when present, must be a positive integer"
                    )
            elif valid_rank:
                ranks.append(rank)
            else:
                errors.append(f"line {lineno}: `rank` is required and must be a positive integer")


            f = rec.get("fact")
            if is_nonempty_str(f):
                facts[" ".join(f.lower().split())] += 1

            if args.repo_root and isinstance(cits, list):
                for c in cits:
                    if not isinstance(c, str):
                        continue
                    # A citation may name several paths (comma/semicolon-separated, possibly
                    # annotated); check EVERY one on disk so a missing non-first path is not
                    # silently skipped.
                    for p in citation_file_paths(c):
                        # A path-like citation that is absolute or escapes the repo with `..` is not
                        # repo-relative, so it is non-portable (useless to other contributors, and may
                        # leak a local checkout path). Surface it distinctly instead of silently
                        # skipping it, but still do NOT probe outside the repo on disk.
                        if os.path.isabs(p) or os.pardir in p.split("/"):
                            msg = f"line {lineno}: cited path is not repo-relative (absolute or uses '..'): {p}"
                            (errors if args.strict_citations else warnings).append(msg)
                            continue
                        if not os.path.exists(os.path.join(args.repo_root, p)):
                            msg = f"line {lineno}: cited path not found on disk: {p}"
                            (errors if args.strict_citations else warnings).append(msg)

    for fact, c in facts.items():
        if c > 1:
            warnings.append(f"duplicate `fact` appears {c}x: {fact[:80]}…")

    rank_counts = Counter(ranks)
    for r in sorted(rr for rr, c in rank_counts.items() if c > 1):
        errors.append(
            f"`rank` {r} is not unique (appears {rank_counts[r]}x); the final "
            f"deliverable requires a unique total ordering with no ties"
        )

    if ranks and len(ranks) == n and not any(c > 1 for c in rank_counts.values()):
        if set(ranks) != set(range(1, n + 1)):
            warnings.append(f"ranks are unique but not a contiguous 1..{n} sequence")

    if n == 0:
        if args.pre_assembly:
            warnings.append("no records in this slice (empty bucket)")
        else:
            errors.append(
                "no records: refusing to validate an empty full-replacement set (it would delete "
                "every existing memory by omission)"
            )

    for w in warnings:
        print(f"WARN  {w}", file=sys.stderr)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)

    print(f"\n{n} records | {len(errors)} errors | {len(warnings)} warnings", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
