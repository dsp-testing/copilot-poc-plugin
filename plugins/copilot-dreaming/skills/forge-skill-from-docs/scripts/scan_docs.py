#!/usr/bin/env python3
"""scan_docs.py — rank a repo's documentation by how *procedural* it is and extract
the raw material a forge step needs to turn the strongest doc into an executable skill.

Unlike the session-history forge skills (creator / friction / timing), this reads the
repo's own checked-in documentation — a local-filesystem read with NO network and NO
session_store_sql, so it is unaffected by the automation firewall and the session-store
data quirks.

A "procedural" doc is one that describes a repeatable operational procedure: ordered
steps, shell commands, prerequisites, verification, and failure modes — i.e. a runbook
that can be distilled into a Conditions/Interface/Policy/Termination (C/R/π/T) skill.
Narrative docs (a README blurb, a changelog) score ~0 and are ignored.

For each candidate it extracts: title, heading outline, fenced command blocks (with the
heading they sit under), and lines flagged as prerequisites or gotchas — so the forge
step can map evidence -> C/R/π/T without re-parsing the markdown.

Output: docs-candidates.json, ranked by procedural score (descending).

Usage:
  scan_docs.py --root <repo_dir> [--glob 'docs/**/*.md' --glob 'README*'] \
               [--topic <keyword>] [--min-score 5] [--out docs-candidates.json]

Pure stdlib. No network. Deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from glob import glob as _glob

DEFAULT_GLOBS = [
    "docs/**/*.md",
    "doc/**/*.md",
    "README*.md",
    "CONTRIBUTING*.md",
    ".github/**/*.md",
]

# Paths to ignore even if a glob matches them: skill definitions are not repo runbooks,
# so a `SKILL.md` (or anything under a skills/.agents/.claude directory) must never be
# mined as a source doc — otherwise this skill would forge from its own definition.
DEFAULT_EXCLUDES = [
    "**/SKILL.md",
    "**/.github/skills/**",
    "**/.agents/**",
    "**/.claude/**",
    "**/node_modules/**",
    "**/vendor/**",
]

# A fenced block is "shell-ish" if tagged as a shell language OR its first
# non-comment line starts with a command-looking token.
SHELL_LANGS = {"sh", "bash", "shell", "console", "zsh", ""}
CMD_START = re.compile(
    r"^\s*(?:\$\s+)?(?:sudo\s+|time\s+)?"
    r"(make|go|git|npm|npx|yarn|pnpm|buf|protoc|python3?|pip3?|docker|"
    r"bazel|cargo|mvn|gradle|terraform|kubectl|gh|curl|bash|sh|mkdir|cp|"
    r"mv|rm|test|echo|export|cd|source|\./)\b"
)

PREREQ_RE = re.compile(
    r"\b(prerequisite|before you|before running|must (?:run|install|be)|"
    r"requires?|you must|first,?\s+(?:run|install)|do this first)\b", re.I)
GOTCHA_RE = re.compile(
    r"\b(gotcha|pitfall|common mistake|caveat|error:|fails?\b|failure|"
    r"if you skip|don'?t\b|do not\b|never\b|stale|out of date)\b", re.I)
VERIFY_RE = re.compile(
    r"\b(verify|verifying|confirm|check that|expect|should print|"
    r"must (?:still )?pass)\b", re.I)
STRUCT_HEADING_RE = re.compile(
    r"\b(procedure|steps?|prerequisite|usage|how to|getting started|"
    r"verif|gotcha|pitfall|do\s*/\s*don'?t|when to|setup|install)\b", re.I)
NUM_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S")
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def iter_doc_paths(root: str, globs: list[str], excludes: list[str]) -> list[str]:
    from fnmatch import fnmatch
    seen: list[str] = []
    for pat in globs:
        for p in _glob(os.path.join(root, pat), recursive=True):
            if not os.path.isfile(p) or p in seen:
                continue
            rel = os.path.relpath(p, root)
            # Normalise to forward slashes so the **/ patterns match on any OS.
            norm = rel.replace(os.sep, "/")
            if any(fnmatch(norm, ex) or fnmatch("/" + norm, ex) for ex in excludes):
                continue
            seen.append(p)
    return sorted(seen)


def parse_blocks(lines: list[str]) -> list[dict]:
    """Extract fenced code blocks with the nearest preceding heading."""
    blocks = []
    cur_heading = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        h = HEADING_RE.match(line)
        if h:
            cur_heading = h.group(2).strip()
            i += 1
            continue
        fence = re.match(r"^\s*```(\w*)\s*$", line)
        if fence:
            lang = fence.group(1).lower()
            body = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            blocks.append({"heading": cur_heading, "lang": lang, "lines": body})
            continue
        i += 1
    return blocks


def block_is_shellish(b: dict) -> bool:
    if b["lang"] in SHELL_LANGS and b["lang"] != "":
        return True
    for ln in b["lines"]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        return bool(CMD_START.match(s))
    return False


def command_lines(b: dict) -> list[str]:
    out = []
    for ln in b["lines"]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^\$\s+", "", s)  # strip a leading prompt
        out.append(s)
    return out


def non_fenced_lines(lines: list[str]) -> list[str]:
    """Return only lines OUTSIDE ``` fenced code blocks (so code comments that start
    with '#' aren't mistaken for markdown headings or numbered steps)."""
    out = []
    in_fence = False
    for l in lines:
        if re.match(r"^\s*```", l):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(l)
    return out


def score_doc(text: str, topic: str | None) -> dict:
    lines = text.splitlines()
    prose = non_fenced_lines(lines)
    blocks = parse_blocks(lines)
    shell_blocks = [b for b in blocks if block_is_shellish(b)]

    cmds: list[str] = []
    prereq_cmds: list[str] = []
    for b in shell_blocks:
        cl = command_lines(b)
        cmds.extend(cl)
        if PREREQ_RE.search(b["heading"]) or re.search(r"prereq|install", b["heading"], re.I):
            prereq_cmds.extend(cl)

    headings = [HEADING_RE.match(l).group(2).strip() for l in prose if HEADING_RE.match(l)]
    struct_headings = [h for h in headings if STRUCT_HEADING_RE.search(h)]
    num_steps = sum(1 for l in prose if NUM_STEP_RE.match(l))
    prereq_hits = len(PREREQ_RE.findall(text))
    gotcha_hits = len(GOTCHA_RE.findall(text))
    verify_hits = len(VERIFY_RE.findall(text))

    # Weighted, capped so no single signal dominates.
    score = 0.0
    score += min(len(shell_blocks), 6) * 3      # commands to run = strongest signal
    score += min(num_steps, 10) * 1             # ordered steps
    score += min(len(struct_headings), 6) * 2   # runbook-shaped structure
    score += min(prereq_hits, 5) * 2            # has prerequisites
    score += min(verify_hits, 5) * 1            # has verification
    score += min(gotcha_hits, 8) * 1            # has failure modes

    topic_match = None
    if topic:
        topic_match = topic.lower() in text.lower()
        if topic_match:
            score += 5
        else:
            score *= 0.25  # heavily deprioritise off-topic docs when a topic is given

    title_m = next((H1_RE.match(l) for l in prose if H1_RE.match(l)), None)
    title = title_m.group(1).strip() if title_m else ""

    gotcha_lines = [l.strip("-* \t").strip()
                    for l in prose if GOTCHA_RE.search(l) and l.strip()][:12]

    return {
        "title": title,
        "score": round(score, 2),
        "signals": {
            "shellBlocks": len(shell_blocks),
            "numberedSteps": num_steps,
            "structuralHeadings": len(struct_headings),
            "prerequisiteHits": prereq_hits,
            "verifyHits": verify_hits,
            "gotchaHits": gotcha_hits,
            "topicMatch": topic_match,
        },
        "headings": headings,
        "commands": cmds[:40],
        "prerequisiteCommands": prereq_cmds[:20],
        "gotchaLines": gotcha_lines,
        "isProcedural": len(shell_blocks) >= 1 and (num_steps >= 1 or len(struct_headings) >= 2),
    }


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "doc-skill"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="repo root to scan")
    ap.add_argument("--glob", action="append", dest="globs", default=None,
                    help="doc glob (repeatable); defaults to docs/**, README, CONTRIBUTING, .github/**")
    ap.add_argument("--topic", default=None,
                    help="optional keyword to prefer (e.g. 'proto', 'submodule')")
    ap.add_argument("--min-score", type=float, default=5.0)
    ap.add_argument("--exclude", action="append", dest="excludes", default=None,
                    help="path pattern to ignore (repeatable); ADDED to the defaults that "
                         "skip SKILL.md and skills/.agents/.claude/node_modules/vendor dirs")
    ap.add_argument("--out", dest="outfile", default="/dev/stdout")
    args = ap.parse_args()

    globs = args.globs or DEFAULT_GLOBS
    excludes = DEFAULT_EXCLUDES + (args.excludes or [])
    paths = iter_doc_paths(args.root, globs, excludes)

    candidates = []
    for p in paths:
        try:
            text = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        c = score_doc(text, args.topic)
        c["path"] = os.path.relpath(p, args.root)
        if c["score"] >= args.min_score and c["isProcedural"]:
            c["suggestedName"] = slugify(c["title"] or os.path.splitext(os.path.basename(p))[0])
            candidates.append(c)

    candidates.sort(key=lambda c: (-c["score"], c["path"]))
    out = {
        "root": os.path.abspath(args.root),
        "scanned": len(paths),
        "topic": args.topic,
        "candidateCount": len(candidates),
        "candidates": candidates,
    }

    dest = sys.stdout if args.outfile == "/dev/stdout" else open(args.outfile, "w", encoding="utf-8")
    json.dump(out, dest, indent=2)
    dest.write("\n")
    if dest is not sys.stdout:
        dest.close()
    print(f"scanned {len(paths)} doc(s); {len(candidates)} procedural candidate(s)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
