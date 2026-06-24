#!/usr/bin/env python3
"""Assemble curated bucket slices into the final full-replacement JSONL.

Repo-agnostic. Reads every curate/curated_*.jsonl produced by the per-bucket curation
subagents (SKILL.md §7.3 step 8) and strips DB-only fields.

Dedup
  Merges only EXACT-normalized duplicate facts (identical after lowercasing and punctuation
  collapse) and unioning their citations. It does not attempt fuzzy/semantic dedup: judging
  whether two differently-worded facts mean the same thing requires reading the cited sources, which
  is the §8 holistic review pass's job, not a token-overlap threshold here.

Ranking (two modes)
  Default (intermediate assemblies, before the holistic pass exists):
    Assign a COARSE rank the script can defend rather than a fabricated fine order. Sort by the
    curation tier (the `rank` the subagents emitted, 1 = most critical). Dense-rank that tier key:
    records the script cannot separate share a rank. These ties are intentional and are replaced by
    the holistic pass.

  --explicit-order (the §8 ordering passes):
    The §8 holistic pass (then the refine pass that polishes its head) reads the list and rewrites
    it in strict most→least-critical order. Trust that line order verbatim and number it 1..N — a
    unique total order (no ties) so the injection cutoff is deterministic. The refine pass's output
    is the authoritative final deliverable; the holistic pass's is the intermediate refine polishes.

Authoritative provenance (--default-interaction-id / --default-agent / --default-base-model)
  Subagents never self-report `source`; assembly is its sole author, stamped after dedup. Records are
  stamped with this run's provenance from the --default-* flags (baseModel only when
  --default-base-model is set). Because each stage re-stamps, only the final assembly's defaults
  reach the deliverable.

Fail closed (full-replacement safety)
  The result fully replaces the repo's memory set, so a silently shrunk file deletes good memories by
  omission. Curated slices are produced by the per-bucket curation subagents and should therefore
  contain only valid `repository` memories. Any line that isn't one — a JSON parse error, a
  non-object line, a record with no usable `fact`, a non-`repository` scope, or empty `citations` —
  is treated as a hard error: it signals a crashed, corrupt, or wrong slice that may also have lost
  real records, so assembly names the offending slice and aborts (exit 1) rather than writing a
  partial set. An empty assembled result is likewise a hard error: investigate the named slice and
  re-run that bucket. (A genuinely zero-memory repo simply has nothing to apply.)

  As a softer signal, a slice (curated_*/reviewed_*) that produces zero records is WARNed by name
  (it was given records to process, so emitting none may be a failed/over-pruning subagent) without
  aborting, since an all-trivia bucket can legitimately net to zero.

Usage:
  python3 scripts/assemble-final.py --in-dir curate \
      --out <owner>-<repo>.memories.curated.jsonl \
      [--explicit-order] \
      [--manifest curate/bucket-manifest.json --manifest-stage curate|review|holistic|refine]
"""

import argparse
import collections
import glob
import json
import os
import re
import sys


# Curation criticality tier: 1 = most critical … 5 = minor. A record with no/garbled tier
# defaults to the neutral middle; tiers are clamped to [TIER_MIN, TIER_MAX] before sorting.
TIER_MIN, TIER_MAX, TIER_DEFAULT = 1, 5, 3


DROP = {
    "_prov",
    "_bucket",
}

# Canonical output key order. The final JSONL emits exactly these fields, so it matches the
# documented curated schema and diffs cleanly regardless of the key order or stray fields the
# per-bucket LLM slices happened to produce.
OUT_FIELDS = [
    "subject",
    "fact",
    "citations",
    "reason",
    "source",
    "scope",
    "rank",
]


def project(m):
    return {k: m[k] for k in OUT_FIELDS if k in m}



def norm(f):
    return re.sub(r"[^a-z0-9]+", " ", (f or "").lower()).strip()


def as_int(v, default):
    """Coerce a possibly-malformed tier/rank value (LLM slices may emit strings
    like "5" or "tier 3") to an int, falling back to default."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        if m:
            return int(m.group())
    return default


def as_citations(v):
    """Coerce a possibly-malformed citations value (slices may emit a bare
    string, null, or other non-list type) into a list of non-empty strings."""
    if isinstance(v, str):
        v = [v]
    elif not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, str) and c.strip()]


def as_str(v):
    """Coerce a possibly non-string field (slices may emit null/numbers) to a string."""
    return v if isinstance(v, str) else ""


def stamp_provenance(rec, default_interaction_id, default_agent, default_base_model):
    """Authoritatively set `rec['source']` — subagents never self-report provenance."""
    src = {"interactionId": default_interaction_id, "agent": default_agent}
    if default_base_model:
        src["baseModel"] = default_base_model
    rec["source"] = src



def infer_manifest_stage(patterns):
    """Infer the expected manifest stage from --pattern when --manifest-stage is omitted. Only the
    non-explicit-order stages (curate/review) are inferable; the explicit-order stages (holistic and
    refine) both assemble with --explicit-order and so are indistinguishable here — main() requires an
    explicit --manifest-stage for those."""
    pats = patterns or ["curated_*.jsonl"]
    if any(p.startswith("reviewed_") for p in pats):
        return "review"
    return "curate"


def validate_manifest_filename(value, field, bucket_number=None):
    """Return a safe manifest filename or raise ValueError.

    Manifest entries are written by bucket-pool.py and should name files within --in-dir. Reject
    absolute paths and parent traversal so a corrupt or hand-edited manifest cannot make assembly
    read or stat files outside the run directory."""
    where = f"manifest bucket {bucket_number} `{field}`" if bucket_number else f"manifest `{field}`"
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    if os.path.isabs(value) or "\\" in value or os.pardir in value.split("/"):
        raise ValueError(f"{where} must be relative and must not use '..' or '\\': {value}")
    return value


def manifest_files(manifest, stage):
    """Return the authoritative slice filenames for a manifest stage."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    buckets = manifest.get("buckets")
    if not isinstance(buckets, list):
        raise ValueError("manifest must contain a `buckets` array")
    out = []
    if stage == "curate":
        field_names = ("curatedFile",)
    elif stage == "review":
        field_names = ("reviewedFile",)
    elif stage == "holistic":
        return [
            validate_manifest_filename(
                manifest.get("holisticFile") or "holistic-ordered.jsonl", "holisticFile"
            )
        ]
    elif stage == "refine":
        return [
            validate_manifest_filename(
                manifest.get("refineFile") or "refine-ordered.jsonl", "refineFile"
            )
        ]
    else:
        raise ValueError(f"unknown manifest stage: {stage}")
    for i, bucket in enumerate(buckets, 1):
        if not isinstance(bucket, dict):
            raise ValueError(f"manifest bucket {i} is not an object")
        for field in field_names:
            out.append(validate_manifest_filename(bucket.get(field), field, bucket_number=i))
    seen = set()
    for name in out:
        if name in seen:
            raise ValueError(f"manifest lists duplicate slice filename: {name}")
        seen.add(name)
    return out


def stage_patterns(stage):
    if stage == "curate":
        return ["curated_*.jsonl"]
    if stage == "review":
        return ["reviewed_*.jsonl"]
    if stage in ("holistic", "refine"):
        # No stale-slice scan for the single-file ordering stages: each has exactly one expected
        # slice (the manifest's holisticFile / refineFile), written fresh and overwritten each run,
        # and its sibling intermediates must be tolerated — the holistic and refine outputs coexist in
        # review/, and a staged compact input may sit beside them — unlike curate/review, whose
        # numbered slice sets vary by run and can leave orphaned curated_*/reviewed_* files. The
        # missing-file check below still requires the stage's own file to be present.
        return []
    raise ValueError(f"unknown manifest stage: {stage}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", default="curate")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--explicit-order",
        action="store_true",
        help="treat the input record order as the final most→least-critical total "
        "order (authored by the §8 holistic pass) and number it 1..N positionally, "
        "instead of generating a provisional order from curation tiers",
    )
    ap.add_argument(
        "--pattern",
        action="append",
        help="glob pattern(s) for slice files in --in-dir (repeatable). "
        "Default: curated_*.jsonl. For the review stage pass "
        "--pattern 'reviewed_*.jsonl'. "
        "Ignored when --manifest is given (the manifest determines the file set).",
    )
    ap.add_argument(
        "--default-interaction-id",
        default="memory-mining",
        help="interactionId stamped onto every record's source (orchestrators should pass the "
        "current session's interaction id).",
    )
    ap.add_argument(
        "--default-agent",
        default="memory-mining",
        help="agent stamped onto every record's source.",
    )
    ap.add_argument(
        "--default-base-model",
        default="",
        help="baseModel stamped onto every record's source — the model the mining run authored "
        "memories on (e.g. claude-opus-4.8); omitted from the source when empty/unset. Because each "
        "stage re-stamps, only the final (--explicit-order) assembly's value reaches the deliverable.",
    )
    ap.add_argument(
        "--manifest",
        help="bucket manifest emitted by bucket-pool.py; when provided, assembly uses only "
        "the manifest-listed files and fails on missing or stale unexpected slice files",
    )
    ap.add_argument(
        "--manifest-stage",
        choices=("curate", "review", "holistic", "refine"),
        help="which manifest stage to assemble (curate/review inferred from --pattern when omitted; "
        "the explicit-order stages holistic and refine must be named explicitly)",
    )
    args = ap.parse_args()
    recs = []
    anomalies = 0  # any line in a curated slice that isn't a valid repository memory record
    zero_output = []  # slices that produced no records (possible subagent failure)
    patterns = args.pattern or ["curated_*.jsonl"]
    if args.manifest:
        if args.explicit_order and not args.manifest_stage:
            # holistic and refine both assemble with --explicit-order, so the stage cannot be
            # inferred — require it explicitly rather than silently defaulting to holistic and
            # shipping the pre-refine deliverable (both ordered files coexist in review/).
            print(
                "error: an --explicit-order assembly requires an explicit --manifest-stage "
                "(holistic or refine); it cannot be inferred",
                file=sys.stderr,
            )
            sys.exit(1)
        stage = args.manifest_stage or infer_manifest_stage(patterns)
        if stage in ("holistic", "refine") and not args.explicit_order:
            print(
                f"error: manifest stage {stage!r} requires --explicit-order so the authored "
                "line order becomes the final unique rank",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            with open(args.manifest, encoding="utf-8") as fh:
                manifest = json.load(fh)
            expected_names = manifest_files(manifest, stage)
        except Exception as e:
            print(f"error: could not read manifest {args.manifest}: {e}", file=sys.stderr)
            sys.exit(1)
        expected = [os.path.join(args.in_dir, name) for name in expected_names]
        missing = [path for path in expected if not os.path.exists(path)]
        if missing:
            print(
                f"error: manifest stage {stage!r} is missing {len(missing)} expected slice file(s)",
                file=sys.stderr,
            )
            for path in missing:
                print(f"  !! missing expected slice: {path}", file=sys.stderr)
            sys.exit(1)
        stale = []
        for pat in stage_patterns(stage):
            for path in glob.glob(os.path.join(args.in_dir, pat)):
                if path not in expected:
                    stale.append(path)
        if stale:
            print(
                f"error: manifest stage {stage!r} found {len(stale)} unexpected matching slice "
                "file(s); refusing to assemble stale artifacts",
                file=sys.stderr,
            )
            for path in sorted(stale):
                print(f"  !! unexpected slice: {path}", file=sys.stderr)
            sys.exit(1)
        files = expected
    else:
        files = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(args.in_dir, pat)))
        files = sorted(set(files))
    if not files:
        if args.manifest:
            print(
                f"no slice files listed in manifest {args.manifest} for stage {stage!r}",
                file=sys.stderr,
            )
        else:
            print(f"no slice files matching {patterns} in {args.in_dir}", file=sys.stderr)
        sys.exit(1)
    for fp in files:
        n = 0
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception as e:
                    print(f"  !! {fp}: JSON parse error: {e}", file=sys.stderr)
                    anomalies += 1
                    continue
                if not isinstance(m, dict):
                    print(f"  !! {fp}: line is not a JSON object: {line[:80]}", file=sys.stderr)
                    anomalies += 1
                    continue
                if not isinstance(m.get("fact"), str) or not m["fact"].strip():
                    print(f"  !! {fp}: record has no usable 'fact'", file=sys.stderr)
                    anomalies += 1
                    continue
                sc = m.get("scope")
                if sc is not None and sc != "repository":
                    print(f"  !! {fp}: non-repository scope {sc!r}", file=sys.stderr)
                    anomalies += 1
                    continue
                for k in list(m):
                    if k in DROP:
                        m.pop(k, None)
                m["scope"] = "repository"
                m["citations"] = as_citations(m.get("citations"))
                if not m["citations"]:
                    print(f"  !! {fp}: record has empty/missing 'citations'", file=sys.stderr)
                    anomalies += 1
                    continue
                recs.append(m)
                n += 1
        print(f"  {n:4}  {fp}")
        if n == 0:
            zero_output.append(fp)

    if zero_output:
        print(
            f"WARN: {len(zero_output)} bucket slice(s) produced zero records. A curated/reviewed "
            "bucket was given records to process, so emitting none may mean a crashed, over-pruning, "
            "or wrong-output subagent rather than a legitimately all-trivia bucket. Record these in "
            "the coverage report and confirm each was intentional:",
            file=sys.stderr,
        )
        for fp in zero_output:
            print(f"  !! zero-output bucket: {fp}", file=sys.stderr)

    if anomalies:
        print(
            f"error: aborting — {anomalies} malformed record(s) across the slice files (flagged "
            "above). A curated slice should contain only valid repository memories, so this signals "
            "a crashed, corrupt, or wrong slice that may also have lost real records; a partial set "
            "must not be written. Inspect the flagged slices and re-run that bucket.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not recs:
        print(
            "error: no records assembled — refusing to write an empty full-replacement set (it "
            "would delete every existing memory by omission). Investigate why every slice was empty.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"raw curated total: {len(recs)}")

    kept = []
    seen = {}  # norm(fact) -> kept record, for O(1) exact-normalized dedup

    # Secondary wording tiebreak (applied only after id-preservation below): rank a record's
    # wording quality by reason length (a rough proxy for clarity/completeness — longer isn't
    # strictly better) then its own citation count, to pick which of two normalized-duplicate
    # facts keeps its wording.
    def content_rank(rec):
        return (len(as_str(rec.get("reason"))), len(as_citations(rec.get("citations"))))


    for m in recs:
        dup = seen.get(norm(m["fact"]))
        if dup:
            # Choose the canonical wording for these normalized-duplicate facts by local content
            # quality. `fact` is guaranteed non-empty (load-time check); only adopt subject/reason
            # when the incoming value is a non-empty string so we never downgrade a valid record into
            # one that would fail validate-output.py.
            if content_rank(m) > content_rank(dup):
                dup["fact"] = m["fact"]
                for f in ("subject", "reason"):
                    val = m.get(f)
                    if isinstance(val, str) and val.strip():
                        dup[f] = val
            cset = dup.setdefault("citations", [])
            for c in m.get("citations", []):
                if c not in cset:
                    cset.append(c)
            # Keep the most favorable (most critical) tier either duplicate was given — lower tier
            # number = more critical — defaulting a missing tier to the neutral middle.
            dup["rank"] = min(
                as_int(dup.get("rank"), TIER_DEFAULT), as_int(m.get("rank"), TIER_DEFAULT)
            )
        else:
            seen[norm(m["fact"])] = m
            kept.append(m)
    print(f"after exact-normalized dedup: {len(kept)}")
    for m in kept:
        m.pop("id", None)
        stamp_provenance(
            m, args.default_interaction_id, args.default_agent, args.default_base_model
        )

    # --- Assign rank (see the module docstring for the full rationale) ---
    if args.explicit_order:
        # Final assembly: the §8 holistic reviewer already placed the full list in strict
        # most→least-critical order, and `kept` preserves that line order, so just number it
        # 1..N — a unique total order, no ties.
        for i, m in enumerate(kept, 1):
            m["rank"] = i
        rank_note = f"unique ranks 1..{len(kept)}"
    else:
        # Intermediate assembly: assign a COARSE rank the script can defend. Sort by curation tier
        # (1 = most critical), then dense-rank that tier so records it cannot separate share a rank.
        # The ties are intentional and are replaced by the holistic pass's --explicit-order numbering.
        def tier_of(m):
            return max(
                TIER_MIN, min(TIER_MAX, as_int(m.get("rank"), TIER_DEFAULT))
            )  # curation tier, 1 = most critical

        kept.sort(key=tier_of)
        prev_key, r = object(), 0
        for m in kept:
            key = tier_of(m)
            if key != prev_key:
                r += 1
                prev_key = key
            m["rank"] = r  # dense coarse rank; ties intentional
        rank_note = f"coarse ranks 1..{r}, ties allowed (provisional)"

    with open(args.out, "w", encoding="utf-8") as f:
        for m in kept:
            out = project(m)
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}: {len(kept)} records ({rank_note})")


if __name__ == "__main__":
    main()
