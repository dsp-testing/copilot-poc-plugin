#!/usr/bin/env python3
"""dispatch.py — Turn a stream plan into ready-to-dispatch per-stream mining units.

Given a target repo, this materializes everything a per-stream mining subagent needs: the exact file
read-set for each code stream and a ready-to-append context fragment. The planner resolves the
recursive vs non-recursive scope-token semantics (the error-prone part — mis-replicating it causes
stream overlap or under-coverage), so this consumer never re-parses the human-readable scope text.

By default it runs `plan-streams.sh` itself (against --repo-root), so the orchestrator issues a single
command. It consumes the planner's two outputs:

  - the raw TSV plan (`<kind>\t<id>\t<scope>\t<count>\t<connector>`);
  - the NUL files-relation (`plan-streams.sh --files-out`): per CODE/TEST/DATA stream a record
    `<id>\0<file1>\0...\0<fileN>\0\0`, plus `_mineable` (the ROOT-scoped mineable set) and `_meta`.

It then, into `<run-dir>/<out-dir>/` (default `dispatch/`):

  - coalesces the planner's many tiny CODE streams into bounded `codebatch:N` dispatch units (unioning
    their resolved files), mirroring bucket-pool's small-bucket coalescing;
  - writes, per dispatched unit, `<safeName>.files` (its exact read-set, hot-ordered by recent git
    churn — or a `--hot-files` override) and `<safeName>.context.md` (a ready-to-append dispatch
    fragment: id, kind, scope, REPO_ROOT, read-set pointer, hot files, candidate/coverage/scratch
    output paths, connector);
  - creates each dispatched unit's `<run-dir>/scratch/<safeName>/` for its candidate/coverage output;
  - seeds the coverage ledger via `streams.tsv` (one row per unit; DATA = skipped, not dispatched) and
    writes `dispatch-manifest.json` (the id<->safeName unit index).

It FAILS CLOSED — clean message, exit 2 — when the streams are not a clean partition of the planned
tree: a CODE stream with no files, two streams sharing a file, or a union that does not equal
`_mineable`. That partition check runs on the RAW per-stream files before coalescing, since a coalesced
union would hide an intra-batch overlap, and before anything is written — so overlap or under-coverage
stops the run with no output. A later filesystem error, e.g. an unwritable out-dir, also exits non-zero
with a clean message; by then the dispatch dir may be partially written, so the orchestrator keys off
the exit code.

Usage:
  dispatch.py --repo-root "$MAIN" --run-dir "$RUN_DIR"   # runs the planner + churn; no other flags needed
  # tuning (all optional): --exclude GLOB / --max-streams N (after a planner blow-up), --churn-window SINCE
  # consume mode (supply the planner outputs directly instead of running it):
  dispatch.py --plan plan.tsv --files files.nul --repo-root "$MAIN" --run-dir "$RUN_DIR"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# Stream kinds that resolve to an exact mineable file set (and so partition the planned tree). DATA is
# recorded but never dispatched; ARTIFACT/EXTERNAL are scoped by source, not files.
FILE_KINDS = ("CODE", "TEST", "DATA")
DISPATCHED_KINDS = ("CODE", "TEST", "ARTIFACT", "EXTERNAL")
VALID_KINDS = ("CODE", "TEST", "DATA", "ARTIFACT", "EXTERNAL")

DEFAULT_MAX_DIRS = 25  # cap on dirs per coalesced CODE batch, so a run of tiny dirs can't build an
# unwieldy unit even when their files stay under the budget


def parse_plan(text):
    """Parse the planner TSV into a list of raw stream dicts, preserving order. Fails closed (raises
    ValueError) on a row that is not the expected 5 tab-separated fields, an unknown stream kind, or a
    duplicate stream id (which would collide on a shared safeName and overwrite another unit's
    artifacts)."""
    streams = []
    seen = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            raise ValueError(
                f"plan line {lineno}: expected 5 tab-separated fields "
                f"(kind, id, scope, count, connector), got {len(parts)}: {line!r}"
            )
        kind, sid, scope, count, connector = parts
        if kind not in VALID_KINDS:
            raise ValueError(
                f"plan line {lineno}: unknown stream kind {kind!r} (expected one of {', '.join(VALID_KINDS)})"
            )
        if sid in seen:
            raise ValueError(f"plan line {lineno}: duplicate stream id {sid!r}")
        seen.add(sid)
        streams.append(
            {"kind": kind, "id": sid, "scope": scope, "count": count, "connector": connector}
        )
    return streams


def parse_files_relation(data):
    """Parse the NUL files-relation into {stream-id: [paths]}. Records are
    ``<id>\\0<path1>\\0...\\0<pathN>\\0\\0`` (a trailing empty field terminates each record). Bytes are
    decoded with surrogateescape so any path git emits round-trips, even non-UTF-8 ones."""
    fields = data.split(b"\0")
    rel = {}
    i, n = 0, len(fields)
    while i < n:
        if fields[i] == b"":  # stray/trailing empty field between or after records
            i += 1
            continue
        sid = fields[i].decode("utf-8", "surrogateescape")
        i += 1
        files = []
        while i < n and fields[i] != b"":
            files.append(fields[i].decode("utf-8", "surrogateescape"))
            i += 1
        if i >= n:
            raise ValueError(
                f"files-relation: record for {sid!r} is missing its terminating NUL "
                "(truncated or corrupt)"
            )
        i += 1  # consume the record-terminating empty field
        if sid in rel:
            raise ValueError(f"files-relation: stream id {sid!r} appears twice")
        rel[sid] = files
    return rel


def validate_partition(streams, rel):
    """Validate the RAW streams partition the planned tree, BEFORE any coalescing (a coalesced union
    would hide an intra-batch overlap). Raises ValueError on the first violation with a clean message.

    Checks: every CODE/TEST/DATA stream has a files record whose length matches its `count` and has no
    intra-stream duplicate; a CODE stream is never empty; the file sets are pairwise disjoint; and
    their union equals the planner's `_mineable` set (the ROOT-scoped oracle)."""
    if "_mineable" not in rel:
        raise ValueError(
            "files-relation has no `_mineable` record — pass a --files file written by "
            "`plan-streams.sh --files-out` (older output cannot be partition-checked)"
        )
    owner = {}  # path -> first stream id that claimed it
    union = set()
    for s in streams:
        if s["kind"] not in FILE_KINDS:
            continue
        sid = s["id"]
        if sid not in rel:
            raise ValueError(f"stream {sid!r} ({s['kind']}) has no files-relation record")
        files = rel[sid]
        if not s["count"].isdigit() or len(files) != int(s["count"]):
            raise ValueError(
                f"stream {sid!r}: count {s['count']!r} != {len(files)} resolved file(s) "
                "(planner/relation drift)"
            )
        if len(set(files)) != len(files):
            raise ValueError(f"stream {sid!r}: the same file is listed more than once")
        if s["kind"] == "CODE" and not files:
            raise ValueError(f"stream {sid!r}: a CODE stream resolved to zero files")
        for f in files:
            if f in owner:
                raise ValueError(
                    f"streams {owner[f]!r} and {sid!r} overlap: both claim {f!r} "
                    "(stream sets must be disjoint)"
                )
            owner[f] = sid
            union.add(f)
    mineable_list = rel["_mineable"]
    if len(set(mineable_list)) != len(mineable_list):
        raise ValueError(
            "files-relation: `_mineable` lists the same file more than once (corrupt oracle)"
        )
    mineable = set(mineable_list)
    missing = sorted(mineable - union)
    extra = sorted(union - mineable)
    if missing:
        raise ValueError(
            f"under-coverage: {len(missing)} mineable file(s) are in no stream, e.g. "
            f"{', '.join(repr(m) for m in missing[:5])} — the streams do not cover the planned tree"
        )
    if extra:
        raise ValueError(
            f"over-coverage: {len(extra)} stream file(s) are not in the `_mineable` set, e.g. "
            f"{', '.join(repr(x) for x in extra[:5])} — planner self-inconsistency"
        )


def coalesce(streams, max_files, max_dirs):
    """Pack consecutive small CODE streams into bounded units, preserving order and unioning their
    files; every other kind flushes the current batch and passes through unchanged. The mining-input
    analog of bucket-pool.py's coalescing (the planner *splits* oversized dirs; this *packs* the tiny
    ones). A single-stream batch is emitted unchanged; a multi-stream batch becomes one `codebatch:N`
    unit. File counts are preserved exactly (the partition check guarantees members are disjoint, so a
    batch's unioned file count equals the sum of its members')."""
    out = []
    batch = []
    seq = 0

    def flush():
        nonlocal seq
        if not batch:
            return
        if len(batch) == 1:
            out.append(dict(batch[0], members=[batch[0]["id"]]))
            batch.clear()
            return
        seq += 1
        total = sum(int(s["count"]) for s in batch)
        files = []
        seen = set()
        for s in batch:
            for f in s["files"]:
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        scopes = "; ".join(s["scope"] for s in batch)
        out.append(
            {
                "kind": "CODE",
                "id": f"codebatch:{seq}",
                "scope": f"{len(batch)} dirs ({total} files): {scopes}",
                "count": str(total),
                "connector": "",
                "files": files,
                "members": [s["id"] for s in batch],
            }
        )
        batch.clear()

    for s in streams:
        if s["kind"] == "CODE":
            packed = sum(int(x["count"]) for x in batch)
            if batch and (packed + int(s["count"]) > max_files or len(batch) >= max_dirs):
                flush()
            batch.append(s)
        else:
            flush()
            out.append(dict(s, members=[s["id"]]))
    flush()
    return out


def parse_hot_files(text):
    """Parse a `git log` churn list (``<count> <path>`` lines, as `uniq -c` emits) into {path: rank},
    rank 0 = most churned. Lines that don't match are ignored, so a stray header is harmless."""
    rank = {}
    for line in text.splitlines():
        m = re.match(r"\s*\d+\s+(.+)$", line)
        if m:
            path = m.group(1).strip()
            if path and path not in rank:
                rank[path] = len(rank)
    return rank


def order_by_churn(files, hot_rank):
    """Stable-sort a unit's files most-churned first; unranked files keep their incoming (alphabetical)
    order after the ranked ones."""
    if not hot_rank:
        return list(files)
    big = len(hot_rank)
    return sorted(files, key=lambda f: (hot_rank.get(f, big),))


def safe_names(units):
    """Map each unit id to a filesystem-safe stem ([A-Za-z0-9_]), de-duplicating collisions with a
    numeric suffix (mirrors bucket-pool.py's safe()/safeName manifest mapping so colon/`#` ids never
    become fragile filenames)."""
    used = {}
    mapping = {}
    for u in units:
        base = re.sub(r"[^A-Za-z0-9_]+", "_", u["id"]).strip("_") or "stream"
        name = base
        k = used.get(base, 0)
        while name in mapping.values():
            k += 1
            name = f"{base}_{k}"
        used[base] = k
        mapping[u["id"]] = name
    return mapping


def render_context(unit, safe_name, repo_root, run_dir, out_dir, hot_top, hot_ordered):
    """Render the ready-to-append Markdown dispatch fragment for one dispatched unit."""
    kind = unit["kind"]
    lines = [
        f"## Your assigned stream: `{unit['id']}`",
        "",
        f"- **Kind:** `{kind}` — apply the `{kind}` completeness bar from your brief.",
        f"- **Scope:** {unit['scope']}",
        f"- **Repo root (pinned, read-only):** `{repo_root}`",
    ]
    scratch = os.path.join(run_dir, "scratch", safe_name)
    if unit["kind"] in ("CODE", "TEST"):
        files_path = os.path.join(out_dir, f"{safe_name}.files")
        n = len(unit["files"])
        order_note = "hot-ordered, most-churned first" if hot_ordered else "in repository order"
        lines.append(
            f"- **Read-set ({n} file(s)):** the exact, authoritative file list for this stream is "
            f"`{files_path}` ({order_note}). Read those files to your saturation bar — this is your "
            "read-set; do not re-enumerate the tree."
        )
        if unit["kind"] == "TEST":
            lines.append(
                "- **TEST stream:** a single low-priority skim for testing **conventions** only "
                "(frameworks, layout, how to run/update tests) — not the fixture data itself."
            )
        if hot_top:
            shown = ", ".join(f"`{p}`" for p in hot_top)
            lines.append(f"- **Hottest files (read first):** {shown}")
    else:  # ARTIFACT / EXTERNAL
        lines.append(
            "- **No file read-set:** locate and page the concrete artifacts yourself via the "
            "matching `where-to-look.md` section for this source."
        )
    if unit["connector"]:
        lines.append(
            f"- **Connector-backed (`{unit['connector']}`):** this stream needs a connector that may "
            "not be surfaced to a given subagent. The orchestrator guarantees coverage via a tool "
            "preflight + retry on a fresh subagent (last resort: mine it itself)."
        )
    lines += [
        f"- **Write candidates to:** `{os.path.join(scratch, 'candidates.jsonl')}`",
        f"- **Write your coverage note to:** `{os.path.join(scratch, 'coverage-note.md')}`",
        f"- **Per-stream scratch dir (gh/query output only):** `{scratch}/`",
        "",
    ]
    return "\n".join(lines)


def clear_prior_artifacts(out_dir):
    """Remove dispatch.py's own artifacts from a reused out-dir before writing. A re-run with a smaller
    unit set (e.g. after an `--exclude`) would otherwise leave a stale `<safeName>.files`/`.context.md`
    behind to mislead an orchestrator who reads the directory rather than the manifest. Only dispatch's
    own artifact types are touched; the per-stream `scratch/` dirs (subagent candidates) are left
    untouched so a resumed run keeps completed work."""
    for name in os.listdir(out_dir):
        if name.endswith((".files", ".context.md")) or name in (
            "streams.tsv",
            "dispatch-manifest.json",
        ):
            path = os.path.join(out_dir, name)
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)


def write_unit_artifacts(units, mapping, args, hot_rank):
    """Write per-unit .files + .context.md, the streams.tsv ledger seed, and dispatch-manifest.json
    into the out-dir, and create each dispatched unit's `<run-dir>/scratch/<safeName>/` so the subagent
    can write its candidates/coverage-note without a manual mkdir. Returns the manifest dict. DATA units
    are recorded but not dispatched (no context file, no scratch dir)."""
    out_dir = os.path.join(args.run_dir, args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    clear_prior_artifacts(out_dir)
    manifest_units = []
    ledger_rows = []
    n_dispatched = 0
    for u in units:
        safe = mapping[u["id"]]
        dispatched = u["kind"] in DISPATCHED_KINDS
        status = "dispatched" if dispatched else "skipped"
        entry = {
            "id": u["id"],
            "safeName": safe,
            "kind": u["kind"],
            "scope": u["scope"],
            "count": int(u["count"]) if u["count"].isdigit() else None,
            "connector": u["connector"] or None,
            "status": status,
            "members": u["members"],
        }
        if u["kind"] in ("CODE", "TEST"):
            ordered = order_by_churn(u["files"], hot_rank)
            files_path = os.path.join(out_dir, f"{safe}.files")
            with open(files_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write("".join(p + "\n" for p in ordered))
            entry["filesFile"] = f"{safe}.files"
            entry["nFiles"] = len(ordered)
            hot_top = [p for p in ordered if p in hot_rank][:10]
        else:
            hot_top = []
        if dispatched:
            scratch_dir = os.path.join(args.run_dir, "scratch", safe)
            os.makedirs(scratch_dir, exist_ok=True)
            # hot_ordered is true only if this unit's files were actually reordered by churn
            hot_ordered = bool(hot_top) if u["kind"] in ("CODE", "TEST") else False
            ctx = render_context(
                u, safe, args.repo_root, args.run_dir, out_dir, hot_top, hot_ordered
            )
            ctx_path = os.path.join(out_dir, f"{safe}.context.md")
            with open(ctx_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(ctx)
            entry["contextFile"] = f"{safe}.context.md"
            n_dispatched += 1
        manifest_units.append(entry)
        ledger_rows.append(
            "\t".join([u["kind"], u["id"], u["scope"], u["count"], u["connector"], status])
        )

    with open(
        os.path.join(out_dir, "streams.tsv"), "w", encoding="utf-8", errors="surrogateescape"
    ) as fh:
        fh.write("".join(r + "\n" for r in ledger_rows))
    manifest = {
        "repoRoot": args.repo_root,
        "runDir": args.run_dir,
        "outDir": args.out_dir,
        "counts": {
            "units": len(units),
            "dispatched": n_dispatched,
            "skipped": len(units) - n_dispatched,
        },
        "units": manifest_units,
    }
    manifest_path = os.path.join(out_dir, "dispatch-manifest.json")
    with open(manifest_path, "w", encoding="utf-8", errors="surrogateescape") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return manifest, manifest_path


def read_file(path, ap, label, *, binary=False):
    """Read a caller-supplied input file, failing closed (clean `ap.error`, no traceback) on any
    OSError — a missing path, a directory, or a permission denial — so a bad --plan/--files/--hot-files
    argument keeps the script's traceback-free contract."""
    try:
        if binary:
            with open(path, "rb") as fh:
                return fh.read()
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError as e:
        ap.error(f"{label}: {e}")


def run_planner(args):
    """Default mode: run the sibling plan-streams.sh against --repo-root and return its
    ``(plan_text, files_path)``. The planner reads `git ls-files` from its cwd and never writes into the
    checkout (its files-relation goes under $RUN_DIR/scratch), so the pinned worktree stays pristine.
    A planner fail (e.g. --max-streams blow-up) is propagated verbatim, diagnostic and exit code."""
    planner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan-streams.sh")
    scratch = os.path.join(args.run_dir, "scratch")
    os.makedirs(scratch, exist_ok=True)
    files_path = os.path.join(scratch, "stream-files.nul")
    cmd = ["bash", planner, "--root", args.root or ".", "--files-out", files_path]
    for ex in args.exclude or []:
        cmd += ["--exclude", ex]
    if args.split_threshold is not None:
        cmd += ["--split-threshold", str(args.split_threshold)]
    if args.max_streams is not None:
        cmd += ["--max-streams", str(args.max_streams)]
    proc = subprocess.run(cmd, cwd=args.repo_root, capture_output=True)
    if (
        proc.stderr
    ):  # surface the planner's diagnostics (exclusions, collapses, or a blow-up report)
        sys.stderr.buffer.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    return proc.stdout.decode("utf-8", "surrogateescape"), files_path


def compute_churn(repo_root, window):
    """Rank currently-tracked files by recent commit-touch count (most-churned first) for read-set
    ordering — so the orchestrator needs no separate hot-file command or flag. Counts commits in the
    last `window` that touch each still-tracked path (the documented git-churn ranking). Returns {} on
    any git error (a non-repo --repo-root in consume mode, or a history-less checkout), which leaves
    read-sets in plain repository order."""
    try:
        tracked = set(
            subprocess.run(
                ["git", "-C", repo_root, "ls-files"], capture_output=True, check=True
            ).stdout.splitlines()
        )
        log = subprocess.run(
            [
                "git",
                "-C",
                repo_root,
                "log",
                f"--since={window}",
                "--pretty=format:",
                "--name-only",
                "--no-renames",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    counts = {}
    for line in log.splitlines():
        if line and line in tracked:
            counts[line] = counts.get(line, 0) + 1
    ranked = sorted(
        counts, key=lambda p: (-counts[p], p)
    )  # count desc, path for a stable tie-break
    return {p.decode("utf-8", "surrogateescape"): i for i, p in enumerate(ranked)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Materialize ready-to-dispatch per-stream units from a plan-streams.sh plan."
    )
    ap.add_argument(
        "--repo-root",
        metavar="PATH",
        required=True,
        help="the pinned worktree subagents read, and the planner's working dir ($MAIN)",
    )
    ap.add_argument(
        "--run-dir", metavar="PATH", required=True, help="the run output directory ($RUN_DIR)"
    )
    ap.add_argument(
        "--out-dir",
        metavar="NAME",
        default="dispatch",
        help="dispatch artifacts subdirectory under --run-dir (default: dispatch)",
    )
    ap.add_argument(
        "--hot-files",
        metavar="PATH",
        help="override the built-in churn ranking with a precomputed `<count> <path>` list (e.g. a "
        "numstat- or session-corroborated one)",
    )
    ap.add_argument(
        "--churn-window",
        metavar="SINCE",
        default="6 months",
        help="git-churn window for read-set hot-ordering (a git `--since` value; default '6 months'); "
        "ignored when --hot-files is given",
    )
    ap.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="max files per coalesced CODE batch (default: the planner's split-threshold from _meta)",
    )
    ap.add_argument(
        "--max-dirs",
        type=int,
        default=DEFAULT_MAX_DIRS,
        help=f"max dirs per coalesced CODE batch (default: {DEFAULT_MAX_DIRS})",
    )
    planner = ap.add_argument_group(
        "planner options (default mode: dispatch runs plan-streams.sh against --repo-root)"
    )
    planner.add_argument("--root", metavar="DIR", help="planner --root (default: .)")
    planner.add_argument(
        "--exclude", metavar="GLOB", action="append", help="planner --exclude (repeatable)"
    )
    planner.add_argument(
        "--split-threshold", type=int, metavar="N", help="planner --split-threshold"
    )
    planner.add_argument("--max-streams", type=int, metavar="N", help="planner --max-streams")
    consume = ap.add_argument_group(
        "consume mode (supply the planner's TSV + files-relation directly instead of running it)"
    )
    consume.add_argument(
        "--plan", metavar="PATH", help="planner TSV (pairs with --files; default stdin)"
    )
    consume.add_argument(
        "--files", metavar="PATH", help="the NUL files-relation from `plan-streams.sh --files-out`"
    )
    args = ap.parse_args()

    if args.max_dirs < 1:
        ap.error("--max-dirs must be >= 1")
    if args.max_files < 0:
        ap.error("--max-files must be >= 0 (0 = take the planner's split-threshold)")
    # --out-dir must name a subdirectory within --run-dir: clear_prior_artifacts() deletes artifacts
    # there, so an absolute path, a `..` escape, or a symlink pointing outside the run could remove
    # files elsewhere. realpath() resolves symlinks before the containment check (abspath would not).
    run_dir_real = os.path.realpath(args.run_dir)
    out_rel = os.path.relpath(
        os.path.realpath(os.path.join(args.run_dir, args.out_dir)), run_dir_real
    )
    if out_rel == os.curdir or out_rel == os.pardir or out_rel.startswith(os.pardir + os.sep):
        ap.error("--out-dir must name a subdirectory within --run-dir")

    planner_opts = (
        args.root is not None
        or args.exclude
        or args.split_threshold is not None
        or args.max_streams is not None
    )
    # Decode text inputs byte-transparently (surrogateescape): git paths are bytes, the scope text and
    # --hot-files can carry non-UTF-8 paths, and strict UTF-8 would crash with a traceback. Outputs are
    # written the same way, so bytes round-trip.
    if args.files is not None:
        # Consume mode: the caller supplies the planner outputs (tests inject crafted plans here).
        if planner_opts:
            ap.error(
                "--root/--exclude/--split-threshold/--max-streams run the planner; omit them with --files"
            )
        if args.plan is not None:
            plan_text = read_file(args.plan, ap, "cannot read --plan")
        else:
            plan_text = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
        rel = read_file(args.files, ap, "cannot read --files", binary=True)
    else:
        # Default: dispatch runs the planner itself, so the orchestrator issues a single command.
        if args.plan is not None:
            ap.error(
                "--plan pairs with --files (consume mode); omit it to have dispatch run the planner"
            )
        if not os.path.isdir(args.repo_root):
            ap.error(f"--repo-root is not a directory: {args.repo_root}")
        try:
            plan_text, files_path = run_planner(args)
        except OSError as e:
            ap.error(f"failed to run the planner under {args.run_dir}: {e}")
        rel = read_file(files_path, ap, "cannot read the planner's files-relation", binary=True)

    if args.hot_files is not None:
        hot_rank = parse_hot_files(read_file(args.hot_files, ap, "cannot read --hot-files"))
    else:
        # By default dispatch computes the churn ranking itself, so --hot-files is optional; it
        # overrides the built-in ranking. Empty (no git history) leaves read-sets in repo order.
        hot_rank = compute_churn(args.repo_root, args.churn_window)

    try:
        streams = parse_plan(plan_text)
        rel = parse_files_relation(rel)
        if not streams:
            raise ValueError("the plan is empty; nothing to dispatch")
        for s in streams:
            if s["kind"] in FILE_KINDS:
                s["files"] = rel.get(s["id"], [])
        validate_partition(streams, rel)
        max_files = args.max_files
        if max_files == 0:
            meta = rel.get("_meta", [])
            st = meta[1] if len(meta) >= 2 and meta[0] == "split-threshold" else None
            max_files = int(st) if st and st.isdigit() else 60
        units = coalesce(streams, max_files, args.max_dirs)
    except ValueError as e:
        ap.error(str(e))

    mapping = safe_names(units)
    try:
        manifest, manifest_path = write_unit_artifacts(units, mapping, args, hot_rank)
    except OSError as e:
        ap.error(
            f"failed to write dispatch artifacts under {os.path.join(args.run_dir, args.out_dir)}: {e}"
        )

    # Defense in depth: the dispatch step fully replaces the orchestrator's manual fan-out, so a
    # missing manifest must stop the run rather than ship a half-written dispatch dir.
    if not os.path.exists(manifest_path):
        ap.error(f"dispatch manifest was not written: {manifest_path}")
    c = manifest["counts"]
    print(
        f"dispatch: {c['units']} unit(s) -> {os.path.join(args.run_dir, args.out_dir)} "
        f"({c['dispatched']} dispatched, {c['skipped']} skipped); manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
