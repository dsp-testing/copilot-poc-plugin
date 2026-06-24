#!/usr/bin/env python3
"""Validate machine-readable repository-memory-miner coverage JSON.

The human `<owner>-<repo>.coverage.md` report is for reviewers; this JSON file is
the script-checkable source of truth for completion gates. It catches the failures
that prose reports can hide: unsupported statuses, placeholders like "pending" or a `<token>`
left unsubstituted (though `queries[]` may contain such tokens as command/search shapes, so long
as the value is a real command and not only tokens),
missing expected artifacts, partial/skipped/no-access/tool-missing sources without an explicit gap,
and non-integer staged candidate counts (rawCandidates, mergedCandidates).

The miner produces NEW candidates only (`candidates.merged.jsonl`); it does not fetch existing
memories or emit a curated deliverable, so there is no existing-memory funnel or curated cross-check
to validate here — the curator owns that downstream.

Exit code 0 = coverage valid; 1 = validation errors; 2 = usage/file errors.

Usage:
  validate-coverage.py coverage.json [--run-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

SOURCE_STATUSES = {"done", "partial", "skipped", "no-access", "tool-missing"}
CODE_STATUSES = {"done", "partial", "skipped"}
COUNT_FIELDS = (
    "rawCandidates",
    "mergedCandidates",
)
PLACEHOLDER_RE = re.compile(
    r"^\s*(?:<[\w -]+>|YYYY-MM-DDTHH:MM:SSZ|_+|pending|tbd|todo|fill me|unknown)\s*$",
    re.I,
)
KNOWN_ANGLE_PLACEHOLDER_RE = re.compile(
    r"<(?:owner|repo|owner-repo|issues-repo|repo-root|timestamp|doc-url|path|bucket|n)>",
    re.I,
)
# Fields recording command/search *templates* (the ledger's "exact searches / queries / IDs" slot),
# where a known angle token like `<n>`/`<repo>` is a deliberate part of the command shape rather than
# an unfilled value. These are exempt from the embedded (anywhere) known-token check — but a value
# that is *only* placeholder tokens (e.g. `"<n>"` or `"<owner>/<repo>"`, with no real command) is
# still rejected. `queries` is the only such field in the schema, and the exemption propagates to its
# descendants, so do not reuse the key for a field that must hold a concrete value.
COMMAND_TEMPLATE_FIELDS = {"queries"}

def is_nonempty_str(v: object) -> bool:
    return isinstance(v, str) and v.strip() != ""


def is_placeholder(v: object, allow_template_tokens: bool = False) -> bool:
    if not isinstance(v, str):
        return False
    if not v.strip() or PLACEHOLDER_RE.match(v):
        return True
    if allow_template_tokens:
        # A command-template field may embed a known token in real command text (e.g.
        # `gh issue view <n>`), but a value that is *only* tokens and separators — nothing concrete
        # left once they are removed — is still an unfilled placeholder.
        return not re.search(r"[A-Za-z0-9]", KNOWN_ANGLE_PLACEHOLDER_RE.sub("", v))
    # Elsewhere, an embedded known token (e.g. `<repo>` inside a half-substituted URL) is a
    # forgotten placeholder.
    return bool(KNOWN_ANGLE_PLACEHOLDER_RE.search(v))


def walk_placeholders(
    value: object, path: str, errors: list[str], in_command_template: bool = False
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            walk_placeholders(
                child,
                f"{path}.{key}",
                errors,
                in_command_template or key in COMMAND_TEMPLATE_FIELDS,
            )
    elif isinstance(value, list):
        for i, child in enumerate(value, 1):
            walk_placeholders(child, f"{path}[{i}]", errors, in_command_template)
    elif is_placeholder(value, allow_template_tokens=in_command_template):
        token = KNOWN_ANGLE_PLACEHOLDER_RE.search(value) if isinstance(value, str) else None
        hint = (
            f" (replace the template token {token.group(0)!r} with the concrete value used)"
            if token
            else ""
        )
        errors.append(f"{path}: placeholder/empty string is not allowed{hint}: {value!r}")


def validate_counts(data: dict, errors: list[str]) -> None:
    counts = data.get("stagedCounts")
    if not isinstance(counts, dict):
        errors.append("$.stagedCounts must be an object")
        return
    for field in COUNT_FIELDS:
        value = counts.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"$.stagedCounts.{field} must be a non-negative integer")


def validate_sources(data: dict, errors: list[str]) -> None:
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("$.sources must be a non-empty array")
        return
    seen = set()
    for i, source in enumerate(sources, 1):
        prefix = f"$.sources[{i}]"
        if not isinstance(source, dict):
            errors.append(f"{prefix} must be an object")
            continue
        sid = source.get("id")
        if not is_nonempty_str(sid):
            errors.append(f"{prefix}.id must be a non-empty string")
        elif sid in seen:
            errors.append(f"{prefix}.id duplicates source id {sid!r}")
        else:
            seen.add(sid)
        status = source.get("status")
        if status not in SOURCE_STATUSES:
            errors.append(
                f"{prefix}.status must be one of {sorted(SOURCE_STATUSES)} (got {status!r})"
            )
        if "empty" in source and not isinstance(source["empty"], bool):
            errors.append(f"{prefix}.empty must be boolean when present")
        if not is_nonempty_str(source.get("whatRead")):
            errors.append(f"{prefix}.whatRead must be a non-empty string")
        queries = source.get("queries")
        if not isinstance(queries, list) or not all(is_nonempty_str(q) for q in queries):
            errors.append(f"{prefix}.queries must be a list of non-empty strings")
        gaps = source.get("gaps")
        if gaps is None:
            gaps = []
        if not isinstance(gaps, list) or not all(is_nonempty_str(g) for g in gaps):
            errors.append(f"{prefix}.gaps must be a list of non-empty strings")
        if status in {"partial", "skipped", "no-access", "tool-missing"} and not gaps:
            errors.append(f"{prefix}.gaps must explain status {status!r}")


def validate_code_streams(data: dict, errors: list[str]) -> None:
    streams = data.get("codeStreams")
    if not isinstance(streams, list) or not streams:
        errors.append("$.codeStreams must be a non-empty array")
        return
    seen = set()
    for i, stream in enumerate(streams, 1):
        prefix = f"$.codeStreams[{i}]"
        if not isinstance(stream, dict):
            errors.append(f"{prefix} must be an object")
            continue
        sid = stream.get("id")
        if not is_nonempty_str(sid):
            errors.append(f"{prefix}.id must be a non-empty string")
        elif sid in seen:
            errors.append(f"{prefix}.id duplicates stream id {sid!r}")
        else:
            seen.add(sid)
        status = stream.get("status")
        if status not in CODE_STATUSES:
            errors.append(
                f"{prefix}.status must be one of {sorted(CODE_STATUSES)} (got {status!r})"
            )
        files = stream.get("files")
        if isinstance(files, bool) or not isinstance(files, int) or files < 0:
            errors.append(f"{prefix}.files must be a non-negative integer")
        for field in ("coveringAgent", "saturationBasis"):
            if not is_nonempty_str(stream.get(field)):
                errors.append(f"{prefix}.{field} must be a non-empty string")
        gaps = stream.get("gaps", [])
        if status == "partial" and (
            not isinstance(gaps, list) or not all(is_nonempty_str(g) for g in gaps) or not gaps
        ):
            errors.append(f"{prefix}.gaps must explain partial coverage")


def validate_artifacts(data: dict, run_dir: str, errors: list[str]) -> None:
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("$.artifacts must be a non-empty array")
        return
    for i, artifact in enumerate(artifacts, 1):
        prefix = f"$.artifacts[{i}]"
        if not is_nonempty_str(artifact):
            errors.append(f"{prefix} must be a non-empty string")
            continue
        if os.path.isabs(artifact) or "\\" in artifact or os.pardir in artifact.split("/"):
            errors.append(f"{prefix} must be relative to --run-dir and must not use '..' or '\\'")
            continue
        if not os.path.exists(os.path.join(run_dir, artifact)):
            errors.append(f"{prefix} not found under --run-dir: {artifact}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate memory-mining coverage JSON.")
    ap.add_argument("json")
    ap.add_argument(
        "--run-dir",
        help="run artifact directory used to resolve artifact paths (default: coverage JSON parent)",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.json):
        print(f"error: file not found: {args.json}", file=sys.stderr)
        return 2
    run_dir = args.run_dir or os.path.dirname(os.path.abspath(args.json))
    if not os.path.isdir(run_dir):
        print(f"error: --run-dir is not a directory: {run_dir}", file=sys.stderr)
        return 2

    try:
        with open(args.json, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("error: coverage JSON must be an object", file=sys.stderr)
        return 2

    errors: list[str] = []
    if data.get("schemaVersion") != 2:
        errors.append("$.schemaVersion must be 2")
    if not is_nonempty_str(data.get("repository")):
        errors.append("$.repository must be a non-empty string")
    if not is_nonempty_str(data.get("runStarted")):
        errors.append("$.runStarted must be a non-empty string")
    walk_placeholders(data, "$", errors)
    validate_counts(data, errors)
    validate_sources(data, errors)
    validate_code_streams(data, errors)
    validate_artifacts(data, run_dir, errors)
    gaps = data.get("gaps")
    if not isinstance(gaps, list) or not all(is_nonempty_str(g) for g in gaps):
        errors.append(
            "$.gaps must be a list of non-empty strings (use [] only when there are no gaps)"
        )

    for err in errors:
        print(f"ERROR {err}", file=sys.stderr)
    print(f"\n{len(errors)} errors", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
