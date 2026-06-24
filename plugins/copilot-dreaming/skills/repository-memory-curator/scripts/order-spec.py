#!/usr/bin/env python3
"""Order, merge, and drop a reviewed memory list via a compact LLM ordering spec.

Two §8 passes author a most->least-critical order plus semantic merges/drops as a small spec over
stable indices, and this script is their shared deterministic front/back end (mirroring
`bucket-pool.py`'s clustering round trip). A `--stage` selects which:

  - `--stage holistic` (the reduce pass): the WHOLE reviewed list, compact view (no `reason`). The
    only semantic-dedup stage; at scale the subagent can neither read the full list (~200 tok/rec)
    nor re-emit it, so it orders/dedups via the spec.
  - `--stage refine` (the refine-the-head pass): only the top `--window` records (default 200),
    full-fidelity view (with `reason`, all citations). It polishes the injected head's uniqueness,
    wording, and order; records below the window keep the holistic order and are carried verbatim.

Modes:
  - `--emit-input PATH` writes the view of the in-scope records, one JSON object per line in list
    order — the subagent's input. Holistic emits `{i, subject, fact, citations, rank}`; refine emits
    `{i, subject, fact, reason, citations}` (full `reason`, uncapped citations, and no `rank` — the
    refine input is already in unique 1..N order). The size summary (and stage-appropriate WARNs) go
    to stderr; only JSONL goes to PATH. In refine, a sibling `PATH.meta.json` records the carried
    tail's identity for the drift guard. It also writes a ready-to-dispatch `<stage>.context.md`
    beside PATH (the input/spec/findings/scratch paths the orchestrator pairs with the rendered
    `<stage>-brief.md`, instead of hand-composing them).
  - `--from-spec PATH` reads the subagent's {merge, reword, drop, order} spec over stable indices and
    materializes the ordered full-record list (e.g. review/holistic-ordered.jsonl,
    review/refine-ordered.jsonl):
      * `merge`: consolidate >=2 records into one. A `keep` index marks a DUPLICATE merge — the
        named member's record is kept verbatim and the others' citations are unioned into it.
        Otherwise the group carries a new `subject`/`fact`/`reason` — a CLOSELY-RELATED merge that
        replaces every member with one new memory (citations unioned).
      * `reword`: rewrite ONE record's `subject`/`fact` in place (by its index `i`) — the
        single-record edit `merge` can't express (it needs >=2 members). Like a consolidate merge it
        is a fresh insert. Its own citations and `reason` are kept — the subagent authors no
        `reason` (it is never injected, and the compact holistic view omits it).
      * `drop`: in holistic, remove low-value records — the intended way to prune the long tail,
        since only the highest-ranked records are ever stored. In refine the below-window tail is
        carried verbatim (the holistic pass already pruned), so a refine `drop` is limited to in-head
        duplicates, low-value facts, or unsafe content.
      * `order` (ranking): the kept records in most->least-critical order; a merged record is named
        by its canonical index — its `keep` member, or the lowest member of a consolidate merge. A
        record leaves the output only via an explicit `drop` or by being merged away. The two stages
        differ on what may go unranked. In holistic, any survivor the spec neither ranks nor drops is
        appended after the ranked records in incoming order — a *backstop* so nothing is lost to
        oversight, not a home for the low-value mass (which should be dropped); as a guard against
        silent downgrades, a global top-`rank` survivor may not fall into this tail, and
        materialize() fails closed if one is left unranked and undropped. Refine has no implicit tail:
        the window is small enough to account for in full, so every record must be ranked, dropped, or
        merged into a ranked record, and materialize() fails closed on any record left unplaced. A
        refine spec that changes nothing at all (no merges, drops, or reordering) is not an error but
        prints a no-op WARNING — a red flag the orchestrator weighs against the pass number and how
        much larger the holistic input was.
    Fails closed on a malformed spec, a duplicate/out-of-range index, or a reviewed list that does
    not match the emitted input / carried tail (drift guard).

The materialized file is a normal slice consumed by `assemble-final.py --explicit-order`, which
numbers its line order 1..N (unique final ranks), unchanged by this script.

Usage:
  # holistic (reduce): emit the whole-list compact input, then materialize the spec
  python3 scripts/order-spec.py --stage holistic --reviewed owner-repo.memories.curated.jsonl \
      --emit-input scratch/holistic/holistic-input.jsonl
  python3 scripts/order-spec.py --stage holistic --reviewed owner-repo.memories.curated.jsonl \
      --from-spec scratch/holistic/holistic-spec.json \
      --input scratch/holistic/holistic-input.jsonl --out review/holistic-ordered.jsonl
  # refine (head): emit the top-window full-fidelity input, then materialize (tail carried verbatim)
  python3 scripts/order-spec.py --stage refine --reviewed owner-repo.memories.curated.jsonl \
      --emit-input scratch/refine/refine-input.jsonl
  python3 scripts/order-spec.py --stage refine --reviewed owner-repo.memories.curated.jsonl \
      --from-spec scratch/refine/refine-spec.json \
      --input scratch/refine/refine-input.jsonl --out review/refine-ordered.jsonl
"""

import argparse
import bisect
import hashlib
import json
import os
import sys


# Citations per record in the compact (holistic) view (token trim). A little more generous than the
# clustering view (bucket-pool's MAX_CLUSTER_CITATIONS) because citation overlap is a strong signal
# of the cross-bucket redundancy that pass exists to catch. Fixed (not a flag) so --emit-input and
# --from-spec always derive the identical view for the drift guard. The refine view is full-fidelity
# and does not cap citations (its window is small, so the budget is not a concern).
MAX_COMPACT_CITATIONS = 6

# Default refine window: how many top records the refine pass polishes at full fidelity. Comfortably
# larger than the injected slice (~20-50) so a merge/drop in the head frees a slot that pulls up a
# record from within the reviewed window rather than from the unreviewed tail below it.
DEFAULT_REFINE_WINDOW = 200

# Injected-slice size (top ~N records ever placed in an agent prompt). If a refine pass drops so much
# of its window that fewer than this many window records survive, unreviewed below-window records
# reach the injected slice — worth a WARN.
INJECTED_SLICE = 50

# Single-context budget for the whole-list (holistic) input. Above it, ordering the whole list in one
# subagent risks exceeding its context — fall back to the shard-and-merge tier (SKILL.md §8). ~600 KB
# is roughly 150K tokens at ~4 bytes/token. The refine window is fixed-size, so this never applies.
HOLISTIC_COMPACT_BYTE_BUDGET = 600_000


def as_citations(v):
    """Coerce a possibly-malformed citations value (a bare string, dict, null, or other non-list
    type) into a list of non-empty strings."""
    if isinstance(v, str):
        v = [v]
    elif not isinstance(v, list):
        return []
    return [c for c in v if isinstance(c, str) and c.strip()]


def read_records(path):
    """Load a JSONL file of memory records, in file order. Fails closed on a malformed line (the
    reviewed list is machine-written, so a parse error means a real problem, not LLM noise)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
            if not isinstance(rec, dict):
                raise ValueError(
                    f"{path}:{lineno}: expected a JSON object, got {type(rec).__name__}"
                )
            out.append(rec)
    return out


def view_record(idx, m, *, full):
    """The view of one record the ordering subagent sees: the fields that drive ordering and dedup.
    Holistic is COMPACT — `reason`/source dropped and citations capped — to fit the whole list in
    one context, and it keeps `rank`: the holistic input's rank is the coarse, possibly-tied curation
    tier, genuinely distinct from the position `i`. Refine is FULL-FIDELITY — `reason` kept and
    citations uncapped — because its window is small and precise wording/dedup decisions want the
    complete record; it omits `rank` because the refine input is already in unique 1..N order.
    --emit-input and --from-spec derive the identical view so the drift guard holds."""
    rec = {
        "i": idx,
        "subject": m.get("subject") or "",
        "fact": m.get("fact") or "",
    }
    if full:
        rec["reason"] = m.get("reason") or ""
    cits = as_citations(m.get("citations"))
    rec["citations"] = cits if full else cits[:MAX_COMPACT_CITATIONS]
    if not full:
        rec["rank"] = m.get("rank")
    return rec


def view_lines(pool, *, full):
    """One view JSON object per line, in pool order — the ordering subagent's input."""
    return [
        json.dumps(view_record(i, m, full=full), ensure_ascii=False) for i, m in enumerate(pool)
    ]


def tail_digest(tail):
    """A stable hash of the carried tail (records below the refine window). The full-list drift guard
    only covers the in-window view the subagent read; refine appends `tail` verbatim, so this lets
    --from-spec confirm the tail is exactly the one --emit-input saw."""
    h = hashlib.sha256()
    for rec in tail:
        h.update(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def meta_path(input_path):
    """Sidecar path next to the emitted --input that records the carried tail's identity (refine)."""
    return input_path + ".meta.json"


def write_tail_meta(input_path, total_records, tail):
    """Record the carried tail's identity beside the emitted --input, so --from-spec can confirm the
    tail it will append verbatim is exactly the one --emit-input saw."""
    with open(meta_path(input_path), "w", encoding="utf-8") as f:
        json.dump({"records": total_records, "tailSha256": tail_digest(tail)}, f)
        f.write("\n")


def verify_tail_meta(input_path, pool, tail):
    """Fail closed unless the carried below-window tail matches what --emit-input saw. materialize()'s
    drift guard already covers the in-window view the subagent read; this covers the verbatim tail it
    never sees, restoring the whole-list fail-closed parity the windowing would otherwise weaken."""
    path = meta_path(input_path)
    if not os.path.exists(path):
        raise ValueError(
            f"refine --from-spec expects the tail sidecar {path!r} written by --emit-input, but it "
            "is missing — re-run --emit-input with the same --stage/--window"
        )
    with open(path, encoding="utf-8") as f:
        try:
            meta = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"could not parse tail sidecar {path}: {e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"tail sidecar {path} must be a JSON object")
    if meta.get("records") != len(pool):
        raise ValueError(
            f"--reviewed has {len(pool)} records but the tail sidecar {path} recorded "
            f"{meta.get('records')!r}; --reviewed changed since --emit-input — re-emit"
        )
    if meta.get("tailSha256") != tail_digest(tail):
        raise ValueError(
            f"the carried below-window tail does not match the one --emit-input saw (per {path}); "
            "--reviewed's tail changed since emit, or --window differs — re-emit"
        )


def emit_input(view_pool, path, *, full, warn_budget):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = "".join(line + "\n" for line in view_lines(view_pool, full=full))
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    nbytes = len(data.encode("utf-8"))
    approx_tokens = nbytes // 4
    print(
        f"ordering input: {path}  ({len(view_pool)} records, {nbytes} bytes, ~{approx_tokens} tokens)",
        file=sys.stderr,
    )
    if warn_budget and nbytes > HOLISTIC_COMPACT_BYTE_BUDGET:
        print(
            f"WARN: the whole-list ordering input is {nbytes} bytes (~{approx_tokens} tokens), above "
            f"the ~{HOLISTIC_COMPACT_BYTE_BUDGET}-byte single-context budget. Ordering the whole list "
            "in one subagent may exceed its context — use the shard-and-merge tier (SKILL.md §8) "
            "instead of a single pass.",
            file=sys.stderr,
        )


# order-spec.py owns the holistic/refine fan-out, so — exactly as dispatch.py does for mining and
# bucket-pool.py for curate/review — `--emit-input` also writes a ready-to-append `<stage>.context.md`
# beside the input it just created, instead of the orchestrator hand-composing those paths every run.
# Every path is absolute: the dispatched subagent's cwd is the invocation checkout, not the run dir.
STAGE_CONTEXT = {
    "holistic": (
        "Holistic ordering + dedup pass (whole list, {n} record(s))",
        "Read the compact list view from",
    ),
    "refine": (
        "Refinement pass — top-window editorial polish ({n} record(s))",
        "Read the full-fidelity head view from",
    ),
}


def _context_md(heading, bullets):
    """Render a per-unit dispatch-context fragment: an H2 heading, a blank line, then the Markdown
    bullets, ending with a single terminating newline. The orchestrator pairs this file with the
    rendered brief at dispatch time."""
    return "## " + heading + "\n\n" + "".join(f"- {b}\n" for b in bullets)


def stage_context_md(stage, count, input_path, scratch_dir):
    """The dispatch context for the single holistic/refine pass: which view to read, where to write
    the ordering spec and the findings note, and the scratch dir — all absolute. The spec/findings
    names match the conventions SKILL.md §8 uses for the matching --from-spec call."""
    heading_tpl, read_label = STAGE_CONTEXT[stage]
    spec = os.path.join(scratch_dir, f"{stage}-spec.json")
    findings = os.path.join(scratch_dir, "findings.md")
    return _context_md(
        heading_tpl.format(n=count),
        [
            f"**{read_label}:** `{input_path}`",
            "**Write your `{merge, reword, drop, order}` spec to:** `" + spec + "`",
            f"**Write your findings note to:** `{findings}`",
            f"**Scratch dir (intermediate files only):** `{scratch_dir}`",
        ],
    )


def _extract_json_object(text):
    """Tolerate an agent that wraps its JSON spec in ``` fences or surrounding prose: return the
    outermost {...} span. (Mirrors bucket-pool.py.)"""
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b > a:
        return s[a : b + 1]
    return s


def _no_dup_keys(pairs):
    """object_pairs_hook that rejects duplicate keys at any nesting level. Plain json.loads silently
    keeps the last duplicate, which would mask an LLM error such as two `order` arrays or a repeated
    field inside a merge object — so fail closed instead. (Mirrors bucket-pool.py.)"""
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"spec has a duplicate JSON key: {k!r}")
        out[k] = v
    return out


def load_spec(path):
    """Parse the subagent's {merge, reword, drop, order} spec, tolerating code fences/prose."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        obj = json.loads(_extract_json_object(raw), object_pairs_hook=_no_dup_keys)
    except json.JSONDecodeError as e:
        raise ValueError(f"could not parse spec JSON in {path}: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("spec must be a JSON object with `merge`, `reword`, `drop`, and `order`")
    unknown = sorted(set(obj) - {"merge", "reword", "drop", "order"})
    if unknown:
        raise ValueError(
            f"spec has unknown top-level key(s): {', '.join(unknown)} "
            "(allowed: merge, reword, drop, order)"
        )
    return obj


def _union_citations(records):
    """Union citations across records, preserving first-seen order."""
    cits = []
    for m in records:
        for c in as_citations(m.get("citations")):
            if c not in cits:
                cits.append(c)
    return cits


def build_dedup_record(members, keeper):
    """Type-1 merge (duplicates): keep `keeper`'s record verbatim, unioning every member's citations
    into it (keeper's first). The other members are dropped, their lineage consolidated away."""
    rec = dict(keeper)
    rec["citations"] = _union_citations([keeper] + [m for m in members if m is not keeper])
    return rec


def build_consolidated_record(members, subject, fact, reason):
    """Type-2 merge (closely related): a NEW memory the subagent authored, replacing every member.
    Citations union across all members; no `id` (a fresh insert); assembly stamps source and the
    final rank."""
    return {
        "subject": subject,
        "fact": fact,
        "citations": _union_citations(members),
        "reason": reason,
        "scope": "repository",
    }


def is_high_value(rec):
    """True if earlier passes gave this record the global top rank; holistic specs may not
    silently relegate such records to the implicit tail."""
    return rec.get("rank") == 1


def reorder_distance(positions):
    """How much a ranking moved records out of their incoming order: the count of ranked records
    whose removal would be needed to leave the rest in ascending incoming-index order (length minus
    the longest increasing subsequence). 0 means the order is unchanged; len - 1 means a full
    reshuffle. Robust to the cascade artifact whereby promoting one record nominally shifts every
    record below it — that reads as a single move, not many."""
    tails = []
    for x in positions:
        i = bisect.bisect_left(tails, x)
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(positions) - len(tails)


def materialize(pool, spec, input_path, *, full, require_full_coverage):
    """Apply a {merge, reword, drop, order} spec to the in-scope pool, returning `(records, n_ranked,
    n_tailed, n_repositioned)` — the last being how many ranked records moved out of incoming order
    (see `reorder_distance`), a magnitude the caller surfaces so the orchestrator can gauge how much
    a refine pass actually reordered.

    Fails closed on a drift between the pool and the emitted input, a malformed spec, an
    out-of-range index, or an index used in more than one role. A record leaves the output only via
    an explicit `drop` or by being merged away.

    With `require_full_coverage` (refine) every record must be ranked, dropped, or merged into a
    ranked record — there is no implicit tail, so any record left unplaced fails closed. Without it
    (holistic) a survivor the spec neither ranks nor drops is appended after the ranked records in
    incoming order — a backstop so nothing is lost to oversight — except that a high-value record
    (`rank` 1) left to that tail fails closed as a silent downgrade."""
    n = len(pool)

    # Drift guard: the in-scope list must be exactly the one the subagent saw, so its indices denote
    # the same records. Re-derive the view and compare to the emitted input.
    expected = view_lines(pool, full=full)
    with open(input_path, encoding="utf-8") as f:
        actual = [line.rstrip("\n") for line in f if line.strip()]
    if actual != expected:
        diff = next(
            (j for j in range(min(len(actual), len(expected))) if actual[j] != expected[j]),
            min(len(actual), len(expected)),
        )
        raise ValueError(
            f"--input {input_path!r} does not match the loaded --reviewed list/window (differs "
            f"at/after record {diff}); pass the same --reviewed and --stage/--window used to emit it, "
            "or re-emit"
        )

    merges = spec.get("merge", [])
    reword = spec.get("reword", [])
    drop = spec.get("drop", [])
    order = spec.get("order", [])
    for name, value in (("merge", merges), ("reword", reword), ("drop", drop), ("order", order)):
        if not isinstance(value, list):
            raise ValueError(f"spec {name!r} must be a list")

    owner = {}  # index -> the role that already claimed it (merge member, drop, or reword)

    def claim(idx, where):
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ValueError(f"{where}: index must be an integer, got {idx!r}")
        if not 0 <= idx < n:
            raise ValueError(f"{where}: index {idx} out of range [0,{n})")
        if idx in owner:
            raise ValueError(f"index {idx} used in two places ({owner[idx]} and {where})")
        owner[idx] = where

    survivors = {}  # key -> materialized record
    survivor_pos = {}  # key -> incoming position (for tail ordering)
    index_to_survivor = {}  # index -> survivor key (canonical merge index or standalone)
    merged_away = {}  # non-canonical merge member -> the merge's canonical index (for error guidance)
    survivor_members = {}  # merge key -> its member indices (for the high-value tail guard)

    for mi, group in enumerate(merges):
        if not isinstance(group, dict):
            raise ValueError(f"merge[{mi}] must be an object with `members`")
        unknown = sorted(set(group) - {"members", "keep", "subject", "fact", "reason"})
        if unknown:
            raise ValueError(
                f"merge[{mi}] has unknown key(s): {', '.join(unknown)} "
                "(allowed: members, keep, subject, fact, reason)"
            )
        members_idx = group.get("members")
        if not isinstance(members_idx, list) or len(members_idx) < 2:
            raise ValueError(f"merge[{mi}] `members` must list >=2 indices, got {members_idx!r}")
        for j in members_idx:
            claim(j, f"merge[{mi}]")
        # Normalize members to stable pool-index order so the citation union doesn't depend on the
        # spec's (LLM-authored) member ordering.
        members = [pool[j] for j in sorted(members_idx)]
        key = ("m", mi)
        keep = group.get("keep")
        authored = sorted(k for k in ("subject", "fact", "reason") if k in group)
        if keep is not None:
            # Type 1 (duplicates): keep one member's record verbatim.
            if authored:
                raise ValueError(
                    f"merge[{mi}] sets `keep` (keep a duplicate) and must not also author "
                    f"{'/'.join(authored)} — drop those, or omit `keep` to write a new memory"
                )
            if isinstance(keep, bool) or not isinstance(keep, int) or keep not in members_idx:
                raise ValueError(
                    f"merge[{mi}] `keep` must be one of its members {members_idx!r}, got {keep!r}"
                )
            survivors[key] = build_dedup_record(members, pool[keep])
        else:
            # Type 2 (closely related): author a new memory that replaces every member.
            for name in ("subject", "fact", "reason"):
                val = group.get(name)
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(
                        f"merge[{mi}] writes a new consolidated memory, so `{name}` must be a "
                        "non-empty string (or set `keep` to keep a member's record instead)"
                    )
            survivors[key] = build_consolidated_record(
                members, group["subject"], group["fact"], group["reason"]
            )
        # A merge is referenced in `order` (and positioned in the tail) by a single canonical index:
        # the `keep` member for a dedup, or the lowest member for a consolidate. The other members
        # are consumed — recorded in `merged_away` so a stray reference fails closed with guidance.
        canonical = keep if keep is not None else min(members_idx)
        survivor_pos[key] = canonical
        index_to_survivor[canonical] = key
        survivor_members[key] = members_idx
        for j in members_idx:
            if j != canonical:
                merged_away[j] = canonical

    for di, idx in enumerate(drop):
        claim(idx, f"drop[{di}]")

    for ri, edit in enumerate(reword):
        if not isinstance(edit, dict):
            raise ValueError(f"reword[{ri}] must be an object with `i` and a new `subject`/`fact`")
        unknown = sorted(set(edit) - {"i", "subject", "fact"})
        if unknown:
            raise ValueError(
                f"reword[{ri}] has unknown key(s): {', '.join(unknown)} "
                "(allowed: i, subject, fact — `reason` is carried from the original, not authored)"
            )
        idx = edit.get("i")
        claim(idx, f"reword[{ri}]")
        for name in ("subject", "fact"):
            val = edit.get(name)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    f"reword[{ri}] rewrites record {idx} into a new memory, so `{name}` must be a "
                    "non-empty string"
                )
        if edit["fact"] == pool[idx].get("fact"):
            raise ValueError(
                f"reword[{ri}] does not change record {idx}'s `fact` (it is byte-identical) — a "
                "reword that keeps the fact only churns identity for nothing. Omit it to "
                "keep the record unchanged, or change the `fact`."
            )
        # A reworded record changes its `fact`, so — like a consolidate merge — it is a fresh insert.
        # The agent authors only `subject`/`fact`; `reason` is never injected (a future agent sees only `fact`)
        # and the compact holistic view omits it, so the original record's `reason` is carried over
        # rather than re-authored blind. It reuses its own index/citations and ranks like a standalone.
        key = ("s", idx)
        survivors[key] = build_consolidated_record(
            [pool[idx]], edit["subject"], edit["fact"], pool[idx].get("reason", "")
        )
        survivor_pos[key] = idx
        index_to_survivor[idx] = key

    for i in range(n):
        if i not in owner:  # not a merge member, not dropped, not reworded
            key = ("s", i)
            rec = dict(pool[i])
            survivors[key] = rec
            survivor_pos[key] = i
            index_to_survivor[i] = key

    head_keys = []
    seen = set()
    for pos, ref in enumerate(order):
        if isinstance(ref, bool) or not isinstance(ref, int):
            raise ValueError(f"order[{pos}]: must be an integer index, got {ref!r}")
        if not 0 <= ref < n:
            raise ValueError(f"order[{pos}]: index {ref} out of range [0,{n})")
        key = index_to_survivor.get(ref)
        if key is None:
            if ref in merged_away:
                raise ValueError(
                    f"order[{pos}]: index {ref} is merged into the group ranked by index "
                    f"{merged_away[ref]} — reference that merge by index {merged_away[ref]}, not {ref}"
                )
            raise ValueError(f"order[{pos}]: index {ref} was dropped and cannot be ranked")
        if key in seen:
            raise ValueError(
                f"order[{pos}]: the survivor for index {ref} is already ranked earlier"
            )
        seen.add(key)
        head_keys.append(key)

    # Records the spec neither ranked, dropped, nor merged away — left to the implicit tail.
    unplaced = [key for key in survivors if key not in seen]

    if require_full_coverage:
        # Refine has no implicit tail: the window is small enough to account for in full, so every
        # record must be ranked, dropped, or merged into a record that is ranked. A standalone left
        # unranked, or a merge whose canonical index is never ranked, means part of the window went
        # unreviewed — fail closed so the omission can't ship.
        if unplaced:
            idxs = ", ".join(
                str(survivor_pos[k]) for k in sorted(unplaced, key=lambda k: survivor_pos[k])
            )
            raise ValueError(
                f"index/indices {idxs}: every record in the window must be ranked in `order`, "
                "dropped, or merged into a record you rank — this pass has no implicit tail, so a "
                "record left unplaced is an error. Add each one to `order` or `drop`."
            )
    else:
        # Holistic keeps a backstop tail, but a record earlier passes placed at the global top must
        # not fall into it by omission — whether as a standalone survivor or kept inside an unranked
        # merge. A consciously *dropped* high-value record is gone by intent; a *merged* one survives,
        # so its placement must be made explicit too, or it silently lands in the tail.
        silently_tailed = []
        for key in unplaced:
            kind, i = key
            if kind == "s" and is_high_value(pool[i]):
                silently_tailed.append(i)
            elif kind == "m" and any(is_high_value(pool[j]) for j in survivor_members[key]):
                silently_tailed.append(survivor_pos[key])  # name the merge by its canonical index
        if silently_tailed:
            idxs = ", ".join(str(i) for i in sorted(silently_tailed))
            raise ValueError(
                f"index/indices {idxs}: high-value record(s) (the global top `rank` 1) would be "
                "silently relegated to the implicit tail — left unranked directly, or kept inside an "
                "unranked merge. Earlier passes judged these important — rank each one (or its merge) "
                "explicitly, or drop it."
            )

    n_repositioned = reorder_distance([survivor_pos[k] for k in head_keys])
    tail_keys = sorted(unplaced, key=lambda k: survivor_pos[k])
    return (
        [survivors[k] for k in head_keys + tail_keys],
        len(head_keys),
        len(tail_keys),
        n_repositioned,
    )


def write_records(records, path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--stage",
        required=True,
        choices=("holistic", "refine"),
        help="which §8 pass this is: 'holistic' orders the WHOLE list with a compact view; 'refine' "
        "polishes the top --window records at full fidelity (with `reason`, all citations) and "
        "carries everything below the window verbatim in holistic order",
    )
    ap.add_argument(
        "--reviewed",
        required=True,
        metavar="PATH",
        help="the assembled memory list (full records, in rank order) — the source for both the "
        "emitted view and the materialized output. For refine this is the holistic-ordered "
        "deliverable (ranks 1..N).",
    )
    ap.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="N",
        help="refine only: how many top records to polish at full fidelity (default "
        f"{DEFAULT_REFINE_WINDOW}); records below it keep the holistic order. Not valid for "
        "--stage holistic (which always orders the whole list).",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--emit-input",
        metavar="PATH",
        help="write the per-stage view of the in-scope records to PATH and exit, then feed it to one "
        "ordering subagent: holistic emits {i, subject, fact, citations, rank}; refine emits "
        "{i, subject, fact, reason, citations} and also writes PATH.meta.json recording the carried "
        "tail.",
    )
    mode.add_argument(
        "--from-spec",
        metavar="PATH",
        help="read the subagent's {merge, reword, drop, order} JSON from PATH and materialize the "
        "ordered full-record list to --out; requires --input and fails closed on a malformed spec, a "
        "bad index, or a --reviewed list/tail that does not match what --emit-input saw",
    )
    ap.add_argument(
        "--input",
        metavar="PATH",
        help="the --emit-input file the subagent read; --from-spec re-derives it from --reviewed to "
        "verify the in-scope view has not drifted",
    )
    ap.add_argument(
        "--out",
        metavar="PATH",
        help="--from-spec: write the materialized ordered list to PATH",
    )
    args = ap.parse_args()

    is_holistic = args.stage == "holistic"
    full = not is_holistic  # refine sees the full-fidelity view (reason + uncapped citations)
    if is_holistic:
        if args.window is not None:
            ap.error(
                "--window applies only to --stage refine; holistic always orders the whole list"
            )
        window = 0
    else:
        window = DEFAULT_REFINE_WINDOW if args.window is None else args.window
        if window < 1:
            ap.error("--window must be >= 1 for refine (it is the size of the head to polish)")

    if not os.path.exists(args.reviewed):
        ap.error(f"--reviewed file not found: {args.reviewed}")
    try:
        pool = read_records(args.reviewed)
    except ValueError as e:
        ap.error(str(e))
    if not pool:
        ap.error(f"--reviewed {args.reviewed} has no records; nothing to order")

    # Holistic forces window == 0 (the whole list, no carried tail); refine uses window >= 1. With a
    # window the subagent sees pool[:window] and the tail pool[window:] is carried verbatim (in
    # holistic order) after the materialized window.
    windowed = window > 0
    view_pool = pool[:window] if windowed else pool
    tail = pool[window:] if windowed else []

    if args.emit_input:
        if args.input or args.out:
            ap.error("--input/--out are only used with --from-spec")
        emit_input(view_pool, args.emit_input, full=full, warn_budget=is_holistic)
        if windowed:
            write_tail_meta(args.emit_input, len(pool), tail)
        input_abs = os.path.abspath(args.emit_input)
        scratch = os.path.dirname(input_abs) or "."
        context_path = os.path.join(scratch, f"{args.stage}.context.md")
        with open(context_path, "w", encoding="utf-8") as fh:
            fh.write(stage_context_md(args.stage, len(view_pool), input_abs, scratch))
        print(f"dispatch context: {context_path}", file=sys.stderr)
        return

    # --from-spec
    if not args.input:
        ap.error("--from-spec requires --input (the emitted view file the subagent read)")
    if not args.out:
        ap.error("--from-spec requires --out (where to write the materialized list)")
    for label, path in (("--from-spec", args.from_spec), ("--input", args.input)):
        if not os.path.exists(path):
            ap.error(f"{label} file not found: {path}")
    try:
        if windowed:
            verify_tail_meta(args.input, pool, tail)
        spec = load_spec(args.from_spec)
        records, n_ranked, n_tailed, n_repositioned = materialize(
            view_pool, spec, args.input, full=full, require_full_coverage=not is_holistic
        )
    except ValueError as e:
        ap.error(str(e))
    # Carry the below-window tail verbatim (refine); assemble-final restamps ranks.
    carried = [dict(rec) for rec in tail]
    out_records = records + carried
    write_records(out_records, args.out)
    n_merges = len(spec.get("merge", []))
    n_rewords = len(spec.get("reword", []))
    n_drops = len(spec.get("drop", []))
    if windowed:
        # Refine enforces full coverage, so there is no in-window implicit tail (n_tailed is always
        # 0 here); report only the verbatim below-window carry.
        kept_desc = f"{len(carried)} below-window carried"
    else:
        kept_desc = f"{n_tailed} kept in incoming order"
    print(
        f"{args.stage}-ordered: {args.out}  ({len(out_records)} records from {len(pool)} reviewed; "
        f"{n_ranked} ranked ({n_repositioned} repositioned), {kept_desc}, "
        f"{n_merges} merge group(s), {n_rewords} reword(s), {n_drops} drop(s))",
        file=sys.stderr,
    )
    if n_tailed and args.stage == "holistic":
        print(
            f"WARNING: {n_tailed} record(s) were neither ranked nor dropped and fell into the "
            "implicit tail (kept in incoming coarse-criticality order). The holistic pass should "
            "rank or drop the whole kept set; a non-trivial tail means records were left unreviewed.",
            file=sys.stderr,
        )
    if tail and len(records) < INJECTED_SLICE:
        print(
            f"WARNING: only {len(records)} of the top-{window} window survived refine (< the "
            f"~{INJECTED_SLICE} injected slice), so unreviewed below-window records now reach the "
            "injected head. Check the spec did not over-drop the window.",
            file=sys.stderr,
        )
    if (
        args.stage == "refine"
        and n_merges == 0
        and n_rewords == 0
        and n_drops == 0
        and n_repositioned == 0
    ):
        print(
            "WARNING: this refine spec made no changes (0 merges, 0 rewords, 0 drops, order unchanged "
            "from the input). Not necessarily an error — a later polish pass, or a head already close "
            "to the whole curated set, can legitimately be near-idempotent — but a first refinement of "
            "a head distilled from a much larger holistic input should normally merge, reword, drop, "
            "or reorder something. Confirm the pass engaged before accepting the result.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
