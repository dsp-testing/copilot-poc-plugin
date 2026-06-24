#!/usr/bin/env python3
"""Group a pool of memory records into bounded, topic-cohesive buckets for parallel curation/review.

A single LLM clustering subagent does the grouping by meaning; this script is the deterministic
front/back end around it:

  - `--emit-cluster-input PATH` writes the compact `{i, subject, fact, citations}` view of the
    (id-deduped) pool, one JSON object per line in pool order — the clustering subagent's input.
  - `--from-assignments PATH` reads the agent's `{label: [indices]}` and materializes the bucket
    files + manifest, failing closed unless the indices are an exact partition of the pool. It
    splits any bucket over `--max-bucket` and coalesces ones too small to be worth a subagent, so no
    bucket exceeds a curation context or is so small that it wastes resources or that related facts
    go un-merged (overly small buckets waste a curation subagent and push work onto the §8 holistic
    pass).

Before either mode, identities are deduplicated so each memory `id` is kept on at most one pooled
record and routes to exactly one bucket (first occurrence wins; a later record keeps its content but
loses its `id`). This is the invariant the review pass relies on, where `--candidates` is the curated
set whose carried-forward records legitimately carry `id`s. At curation, mined candidates already
arrive id-less — `merge-dedup.py` deterministically drops `id`/server-managed fields before the
existing set is pooled in — so here this pass is a fail-loud backstop, not the primary strip.

Usage:
  # 1. emit the compact input for the clustering subagent
  python3 scripts/bucket-pool.py --existing existing.memories.jsonl \
      --candidates candidates.merged.jsonl --emit-cluster-input curate/cluster-input.jsonl
  # 2. (dispatch one clustering subagent; it writes curate/assignments.json)
  # 3. materialize the buckets + manifest from its assignment
  python3 scripts/bucket-pool.py --existing existing.memories.jsonl \
      --candidates candidates.merged.jsonl --from-assignments curate/assignments.json \
      --cluster-input curate/cluster-input.jsonl --out-dir curate --stage curate

Output: curate/bucket_<safeName>.jsonl (one record per line), each record tagged with
`_prov` = "existing" | "new", a ready-to-dispatch curate/<safeName>.context.md per bucket, plus
curate/bucket-manifest.json unless --no-manifest is passed. <safeName> is the bucket label sanitized
to a filename-safe stem (colliding labels get a numeric suffix). The manifest is the authoritative
list of expected curation/review slice filenames for assemble-final.py --manifest. Prints the bucket
sizes.
"""

import argparse
import collections
import hashlib
import json
import os
import re
import sys


def as_citations(v):
    """Coerce a possibly-malformed citations value (slices may emit a bare string,
    dict, null, or other non-list type) into a list of non-empty strings."""
    if isinstance(v, str):
        v = [v]
    elif not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, str) and c.strip()]


def load(path, prov):
    out = []
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception as e:
                print(f"  !! parse error in {path}: {e}", file=sys.stderr)
                continue
            if not isinstance(m, dict):
                print(f"  !! skipping non-object record in {path}", file=sys.stderr)
                continue
            m["citations"] = as_citations(m.get("citations"))
            m["_prov"] = prov
            out.append(m)
    return out


def record_identities(m):
    """The memory identity a record claims: its `id` (an empty set when it carries none). It denotes
    the prior stored memory the record represents, so no two pooled records may share one."""
    pid = m.get("id")
    if isinstance(pid, str) and pid.strip():
        return {pid.strip()}
    return set()


def dedup_identities(pool):
    """Ensure each memory `id` is kept on at most one pooled record, so it is routed to exactly one
    bucket. `pool` lists existing records before candidates (load order), so the first occurrence of
    an `id` wins (an existing memory over a later collision; existing-internal duplicates collapse the
    same way). The live use is the review stage, where `--candidates` is the curated set whose
    carried-forward records legitimately carry `id`s.

    Lossless: a record whose `id` collides with an earlier record keeps its content but has its `id`
    stripped — it becomes an unidentified candidate that fact-dedup and the holistic pass can still
    merge. (At curation, `merge-dedup.py` already drops any mined `id` before pooling, so a candidate
    `id` reaching here is off-contract and this pass is a fail-loud backstop.) bucket-pool catches
    *collisions*; assemble-final.py later drops any *fabricated/stale* id absent from the existing
    set, via filter_ids_for_unchanged_fact.

    Mutates `pool` in place; returns (Counter by provenance, [ids])."""
    seen = set()
    stripped = collections.Counter()
    collided_ids = []
    for m in pool:
        idents = record_identities(m)
        overlap = idents & seen
        if overlap:
            stripped[m.get("_prov") or "unknown"] += 1
            collided_ids.extend(sorted(overlap))
            m.pop("id", None)
        else:
            seen |= idents
    return stripped, collided_ids


MAX_CLUSTER_CITATIONS = 3  # citations per record in the compact clustering input (token trim)
MAX_LABEL_LEN = 48  # cap on an LLM-authored bucket label before it is hash-suffixed
# The cluster brief targets cap/2..cap records per bucket; the coalescing floor is the lower end of
# that range -- the cap divided by this. Buckets below the floor are merged into a sibling.
BUCKET_FLOOR_DIVISOR = 2


def _hard_chunk(records, cap):
    """Slice an indivisible oversized group into the fewest cap-bounded, near-equal pieces.
    Balancing (sizes differ by at most one) avoids a near-full bucket plus a one-record tail,
    which would starve a curation subagent of local context. This is the final size-bound guarantee."""
    n = len(records)
    k = max(1, (n + cap - 1) // cap)  # fewest slices that keep every piece <= cap
    q, r = divmod(n, k)
    out, i = [], 0
    for j in range(k):
        size = q + (1 if j < r else 0)
        out.append(records[i : i + size])
        i += size
    return out


# bucket-pool.py owns the curate/review fan-out, so — exactly as dispatch.py does for mining — it
# writes each bucket a ready-to-append `<safeName>.context.md` the orchestrator pairs with the rendered
# `<stage>-brief.md`, instead of hand-composing the per-bucket paths every run. Every path is absolute:
# a dispatched subagent's cwd is the invocation checkout, not the run dir, so the bare manifest
# filenames would not resolve. `curate` writes a `curated_*` slice, `review` a `reviewed_*` slice.
STAGE_SLICE = {"curate": "curated", "review": "reviewed"}


def _context_md(heading, bullets):
    """Render a per-unit dispatch-context fragment: an H2 heading, a blank line, then the Markdown
    bullets, ending with a single terminating newline. The orchestrator pairs this file with the
    rendered brief at dispatch time."""
    return "## " + heading + "\n\n" + "".join(f"- {b}\n" for b in bullets)


def _display_label(label):
    """Sanitize an agent-authored bucket label for safe display inside the heading's code span:
    collapse any whitespace (including newlines) to single spaces and neutralize backticks, so an
    arbitrary label can never break the Markdown heading or inject a line. The safeName — not this
    label — drives every path, so this is display-only."""
    return " ".join(str(label).split()).replace("`", "'")


def bucket_context_md(stage, key, name, count, out_dir_abs, scratch_dir):
    """The dispatch context for one curate/review bucket: which slice to read, where to write the
    curated/reviewed slice and the findings note, and the per-bucket scratch dir — all absolute."""
    kind = STAGE_SLICE[stage]
    return _context_md(
        f"Your assigned {stage} bucket: `{_display_label(key)}` (`{name}`, {count} record(s))",
        [
            f"**Read your bucket slice from:** `{os.path.join(out_dir_abs, f'bucket_{name}.jsonl')}`",
            f"**Write your {kind} slice to:** `{os.path.join(out_dir_abs, f'{kind}_{name}.jsonl')}`",
            f"**Write your findings note to:** `{os.path.join(scratch_dir, 'findings.md')}`",
            f"**Per-bucket scratch dir (intermediate files only):** `{scratch_dir}`",
        ],
    )


def clear_prior_context(out_dir):
    """Remove bucket-pool's own prior artifacts (`bucket_*.jsonl`, `*.context.md`) from a reused
    out-dir before writing, so a re-cluster cannot leave an orphan slice/context behind to mislead an
    orchestrator that reads the directory rather than the manifest. The subagent outputs
    (`curated_*`/`reviewed_*`) are left untouched so a resumed run keeps completed work."""
    if not os.path.isdir(out_dir):
        return
    for fn in os.listdir(out_dir):
        if (fn.startswith("bucket_") and fn.endswith(".jsonl")) or fn.endswith(".context.md"):
            path = os.path.join(out_dir, fn)
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)


def write_buckets_and_manifest(
    bucket_pairs, pool, out_dir, *, stage, manifest_extra, write_manifest, manifest_path
):
    """Write each (key, records) pair to ``bucket_<safeName>.jsonl``, a ready-to-dispatch
    ``<safeName>.context.md``, and a per-bucket scratch dir, plus (unless suppressed) the bucket
    manifest assemble-final.py --manifest validates. ``stage`` (``curate``/``review``) selects the
    output-slice name, the scratch subdir, and the brief the context is paired with."""
    os.makedirs(out_dir, exist_ok=True)
    clear_prior_context(out_dir)
    out_dir_abs = os.path.abspath(out_dir)
    # --out-dir is always $RUN_DIR/<stage>, so the run dir (hence the scratch root) is its parent.
    scratch_root = os.path.join(os.path.dirname(out_dir_abs), "scratch", stage)

    def safe(k):
        """Make a bucket key safe for use in a filename."""
        return re.sub(r"[^A-Za-z0-9]+", "_", k).strip("_") or "misc"

    seen = {}
    manifest = {
        "schemaVersion": 1,
        "bucketCount": len(bucket_pairs),
        "recordCount": len(pool),
        **manifest_extra,
        "holisticFile": "holistic-ordered.jsonl",
        "refineFile": "refine-ordered.jsonl",
        "buckets": [],
    }
    # Sort by size then key so collision suffixes are assigned deterministically.
    for key, items in sorted(bucket_pairs, key=lambda x: (-len(x[1]), x[0])):
        ex = sum(1 for m in items if m["_prov"] == "existing")
        name = safe(key)
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"  # distinct keys must not share a file
        else:
            seen[name] = 0
        bucket_file = f"bucket_{name}.jsonl"
        with open(os.path.join(out_dir, bucket_file), "w", encoding="utf-8") as f:
            for m in items:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        scratch_dir = os.path.join(scratch_root, name)
        os.makedirs(scratch_dir, exist_ok=True)
        context_file = f"{name}.context.md"
        with open(os.path.join(out_dir, context_file), "w", encoding="utf-8") as f:
            f.write(bucket_context_md(stage, key, name, len(items), out_dir_abs, scratch_dir))
        print(f"{len(items):4}  bucket_{name}  (existing {ex} / new {len(items) - ex})")
        manifest["buckets"].append(
            {
                "key": key,
                "safeName": name,
                "bucketFile": bucket_file,
                "curatedFile": f"curated_{name}.jsonl",
                "reviewedFile": f"reviewed_{name}.jsonl",
                "contextFile": context_file,
                "recordCount": len(items),
                "existingCount": ex,
                "newCount": len(items) - ex,
            }
        )
    print(f"total pooled: {len(pool)}  buckets: {len(bucket_pairs)}")
    if write_manifest:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"manifest: {manifest_path}")


# ---- clustering front/back end ----------------------------------------------------------------
# A single LLM clustering subagent reads a compact view of the whole pool and returns topic buckets
# as {label: [pool indices]}. bucket-pool.py is the deterministic front/back end: it emits that view
# (--emit-cluster-input) and materializes the assignment into the bucket files + manifest
# assemble-final.py validates (--from-assignments). The pool is loaded and id-deduped identically in
# both modes, so an index denotes the same record across the round trip.


def cluster_input_record(idx, m):
    """The token-trimmed view of one record the clustering subagent sees: only the fields that drive
    topic assignment. `reason`/score/votes/timestamps/rank/source are dropped to fit the context."""
    return {
        "i": idx,
        "subject": m.get("subject") or "",
        "fact": m.get("fact") or "",
        "citations": as_citations(m.get("citations"))[:MAX_CLUSTER_CITATIONS],
    }


def cluster_input_lines(pool):
    """One compact JSON object per line, in pool order — the clustering subagent's input."""
    return [json.dumps(cluster_input_record(i, m), ensure_ascii=False) for i, m in enumerate(pool)]


def emit_cluster_input(pool, path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lines = cluster_input_lines(pool)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    print(f"cluster-input: {path}  ({len(lines)} records)")


def _no_dup_keys(pairs):
    """object_pairs_hook that rejects duplicate labels (plain json.load would silently keep the last,
    hiding a whole bucket whose indices then surface only as 'unassigned')."""
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"assignments contain a duplicate bucket label: {k!r}")
        out[k] = v
    return out


def _extract_json_object(text):
    """Tolerate an agent that wraps its JSON in ``` fences or surrounding prose (leading or trailing):
    return the outermost {...} span. Strict parsing on the raw text would fail-closed on harmless
    formatting."""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        return s[a : b + 1]
    return s


def load_assignments(path):
    """Parse the clustering subagent's {label: [indices]} output, tolerating code fences and
    rejecting duplicate labels."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        obj = json.loads(_extract_json_object(raw), object_pairs_hook=_no_dup_keys)
    except json.JSONDecodeError as e:
        raise ValueError(f"could not parse assignments JSON in {path}: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("assignments must be a JSON object mapping a bucket label to [indices]")
    return obj


def _safe_label(label):
    """Coerce an LLM-authored bucket label into a bounded, non-empty key (the filename charset is
    applied later by write_buckets_and_manifest)."""
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"bucket label must be a non-empty string, got {label!r}")
    label = label.strip()
    if len(label) > MAX_LABEL_LEN:
        digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
        label = label[:MAX_LABEL_LEN].rstrip() + "_" + digest
    return label


def _coalesce_small_buckets(pairs, cap):
    """Backstop for the clusterer's own size-balancing (symmetric with the cap split below):
    first-fit-pack buckets below the floor among *themselves* into <= cap bins, concatenating their
    labels with `_`, and leave fuller buckets untouched. Tiny buckets are not merely wasteful — they
    scatter related facts across the fleet so overlaps go un-merged at curation, which bloats the
    holistic pass's input pool and erodes the signal in the injected top slice. Packing only the
    small buckets together (never into the coherent ones) preserves the topics the clusterer built."""
    floor = max(1, cap // BUCKET_FLOOR_DIVISOR)
    small = [(k, recs) for k, recs in pairs if len(recs) < floor]
    if len(small) < 2:
        return pairs  # a lone small bucket is left as the agent placed it; nothing to merge
    kept = [(k, recs) for k, recs in pairs if len(recs) >= floor]
    bins = []  # each: [list of source labels, combined records]
    for key, recs in sorted(small, key=lambda kr: -len(kr[1])):
        for b in bins:
            if len(b[1]) + len(recs) <= cap:
                b[0].append(key)
                b[1].extend(recs)
                break
        else:
            bins.append([[key], list(recs)])
    coalesced = [(_safe_label("_".join(labels)), recs) for labels, recs in bins]
    print(
        f"coalesced {len(small)} small buckets (< {floor} records) into {len(coalesced)}",
        file=sys.stderr,
    )
    return kept + coalesced


def materialize_from_assignments(pool, assignments, cap, cluster_input_path):
    """Turn the agent's {label: [indices]} into (key, records) bucket pairs, failing closed unless
    the indices are an exact partition of the pool, and enforcing the per-bucket cap."""
    n = len(pool)

    # Alignment guard: the pool we just loaded must be exactly the one the agent clustered. The
    # exact-partition check below only catches a different record *count*; re-deriving the compact
    # view and comparing it to the emitted input also catches same-count drift in any
    # clustering-relevant field — subject/fact/top citations — (e.g. a resumed run that regenerated
    # candidates between emit and materialize).
    expected = cluster_input_lines(pool)
    with open(cluster_input_path, encoding="utf-8") as f:
        actual = [line.rstrip("\n") for line in f if line.strip()]
    if actual != expected:
        diff = next(
            (j for j in range(min(len(actual), len(expected))) if actual[j] != expected[j]),
            min(len(actual), len(expected)),
        )
        raise ValueError(
            f"--cluster-input {cluster_input_path!r} does not match the loaded pool (differs "
            f"at/after record {diff}); pass the same --existing/--candidates used to emit it, or "
            "re-emit and re-cluster"
        )

    seen = {}
    raw_pairs = []
    for label, idxs in assignments.items():
        key = _safe_label(label)
        if not isinstance(idxs, list):
            raise ValueError(
                f"bucket {label!r} must map to a list of indices, got {type(idxs).__name__}"
            )
        recs = []
        for x in idxs:
            if isinstance(x, bool) or not isinstance(x, int):
                raise ValueError(f"bucket {label!r} has a non-integer index: {x!r}")
            if not 0 <= x < n:
                raise ValueError(f"bucket {label!r} index {x} is out of range [0,{n})")
            if x in seen:
                raise ValueError(f"index {x} assigned to two buckets ({seen[x]!r} and {label!r})")
            seen[x] = label
            recs.append(pool[x])
        if recs:
            raw_pairs.append((key, recs))
    missing = [i for i in range(n) if i not in seen]
    if missing:
        head = ", ".join(map(str, missing[:20])) + (
            f", …(+{len(missing) - 20} more)" if len(missing) > 20 else ""
        )
        raise ValueError(
            f"assignments are not a partition: {len(missing)} record(s) unassigned: {head}"
        )

    # Enforce the cap: split any oversized bucket into balanced, cap-bounded pieces (keeps the
    # agent's semantic grouping for in-range buckets but never hands a curate/review subagent more
    # records than its context holds — an over-context subagent could silently drop the overflow).
    pairs = []
    for key, recs in raw_pairs:
        if len(recs) > cap:
            pieces = _hard_chunk(recs, cap)
            print(
                f"split oversized bucket {key!r} ({len(recs)} > --max-bucket {cap}) into "
                f"{len(pieces)} pieces; consider re-clustering with a tighter target",
                file=sys.stderr,
            )
            pairs.extend((key, piece) for piece in pieces)
        else:
            pairs.append((key, recs))
    # Then coalesce the other extreme — tiny buckets — back toward the cap (see the docstring).
    return _coalesce_small_buckets(pairs, cap)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--existing")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out-dir", default="curate")
    # Default ~100 records ≈ a single curation subagent can verify one bucket without exceeding
    # its context; raise for short records / cheaper runs, lower if curation quality suffers.
    ap.add_argument("--max-bucket", type=int, default=100)
    manifest_group = ap.add_mutually_exclusive_group()
    manifest_group.add_argument(
        "--manifest",
        help="manifest output path (default: <out-dir>/bucket-manifest.json)",
    )
    manifest_group.add_argument(
        "--no-manifest",
        action="store_true",
        help="do not write a bucket manifest",
    )
    ap.add_argument(
        "--emit-cluster-input",
        metavar="PATH",
        help="write the compact {i, subject, fact, citations} clustering input (one JSON object per "
        "line, in pool order) to PATH and exit; feed it to one clustering subagent",
    )
    ap.add_argument(
        "--from-assignments",
        metavar="PATH",
        help="read the clustering subagent's {label: [indices]} JSON from PATH and materialize the "
        "bucket files + manifest; requires --cluster-input and fails closed unless the indices are "
        "an exact partition of the pool",
    )
    ap.add_argument(
        "--cluster-input",
        metavar="PATH",
        help="the --emit-cluster-input file the clustering subagent read; --from-assignments uses "
        "it to verify the loaded pool matches what was clustered",
    )
    ap.add_argument(
        "--stage",
        choices=("curate", "review"),
        help="required with --from-assignments: which fan-out this materializes. Selects each "
        "bucket's output-slice name (curated_*/reviewed_*) and scratch subdir (scratch/<stage>/), "
        "and names the brief its written <safeName>.context.md is dispatched with.",
    )
    args = ap.parse_args()

    for label, path in (("--existing", args.existing), ("--candidates", args.candidates)):
        if path and not os.path.exists(path):
            ap.error(f"{label} file not found: {path}")
    pool = load(args.existing, "existing") + load(args.candidates, "new")
    if not pool:
        ap.error("no records loaded from --existing/--candidates; nothing to bucket")
    stripped, collided_ids = dedup_identities(pool)
    if stripped:
        by_prov = ", ".join(f"{prov} {n}" for prov, n in sorted(stripped.items()))
        shown = sorted(set(collided_ids))
        id_note = ", ".join(shown[:20]) + (
            f", …(+{len(shown) - 20} more)" if len(shown) > 20 else ""
        )
        print(
            f"id-dedup: stripped the identity from {sum(stripped.values())} record(s) ({by_prov}) "
            "that reused an id already claimed earlier in the pool, so each id routes to exactly one "
            f"bucket; affected id(s): {id_note}",
            file=sys.stderr,
        )
    if args.max_bucket < 1:
        ap.error("--max-bucket must be >= 1")

    if bool(args.emit_cluster_input) == bool(args.from_assignments):
        ap.error("choose exactly one of --emit-cluster-input or --from-assignments")
    if args.cluster_input and not args.from_assignments:
        ap.error("--cluster-input is only used with --from-assignments")
    if args.stage and not args.from_assignments:
        ap.error("--stage is only used with --from-assignments")
    if args.from_assignments and not args.stage:
        ap.error(
            "--from-assignments requires --stage {curate,review} (it selects the output-slice name "
            "and per-bucket scratch dir, and names the brief the context is dispatched with)"
        )

    if args.emit_cluster_input:
        emit_cluster_input(pool, args.emit_cluster_input)
        return

    if not args.cluster_input:
        ap.error("--from-assignments requires --cluster-input (the emitted input the agent read)")
    for label, path in (
        ("--from-assignments", args.from_assignments),
        ("--cluster-input", args.cluster_input),
    ):
        if not os.path.exists(path):
            ap.error(f"{label} file not found: {path}")
    try:
        assignments = load_assignments(args.from_assignments)
        bucket_pairs = materialize_from_assignments(
            pool, assignments, args.max_bucket, args.cluster_input
        )
    except ValueError as e:
        ap.error(str(e))
    manifest_path = args.manifest or os.path.join(args.out_dir, "bucket-manifest.json")
    # Drop any stale manifest first — always, even under --no-manifest. The post-write check below
    # then proves THIS run wrote a fresh one; and because --from-assignments clears the prior buckets,
    # a leftover manifest from an earlier with-manifest run would otherwise survive pointing at slices
    # that no longer exist, misleading assembly despite the explicit "no manifest" intent.
    if os.path.exists(manifest_path):
        os.remove(manifest_path)
    write_buckets_and_manifest(
        bucket_pairs,
        pool,
        args.out_dir,
        stage=args.stage,
        manifest_extra={"maxBucket": args.max_bucket},
        write_manifest=not args.no_manifest,
        manifest_path=manifest_path,
    )
    # Defense in depth: the normal flow must leave a manifest for assemble-final.py --manifest.
    if not args.no_manifest and not os.path.exists(manifest_path):
        sys.exit(f"error: bucket manifest was not written: {manifest_path}")


if __name__ == "__main__":
    main()
