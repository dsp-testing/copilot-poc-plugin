#!/usr/bin/env python3
"""validate_skill.py — gate a Forge-generated SKILL.md before promotion.

Enforces the contract the forge-create-skill algorithm requires (ported from
github/copilot-agent-runtime branch golaraj/forge-agent-concept, .github/skills/
forge-create-skill/SKILL.md "Validation gate"):

  Frontmatter (required): name (kebab-case, matches dir), description, generated-by
  Body (required sections, the C/R/π/T formal model):
    # <title>, ## Purpose, ## Conditions (C), ## Interface (R), ## Policy (π),
    ## Termination (T), ## Always do, ## Never do, ## Scope boundaries

Exit 0 = valid; 1 = at least one hard failure; 2 = usage error.

Usage: validate_skill.py <path/to/SKILL.md>
"""

from __future__ import annotations

import os
import re
import sys

REQUIRED_FM = ["name", "description", "generated-by"]
VALID_CATEGORIES = {
    "codegen", "build-verify", "lint-gate", "dev-env",
    "triage", "navigation", "debug", "ceremony", "general",
}
REQUIRED_SECTIONS = [
    "## Purpose",
    "## Conditions (C)",
    "## Interface (R)",
    "## Policy (π)",
    "## Termination (T)",
    "## Always do",
    "## Never do",
    "## Scope boundaries",
]
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_skill.py <SKILL.md>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"error: not found: {path}", file=sys.stderr)
        return 2

    text = open(path, encoding="utf-8").read()
    errors: list[str] = []

    fm = parse_frontmatter(text)
    if not fm:
        errors.append("missing or malformed YAML frontmatter")
    for key in REQUIRED_FM:
        if not fm.get(key):
            errors.append(f"frontmatter missing required key: {key}")

    name = fm.get("name", "")
    if name and not KEBAB.match(name):
        errors.append(f"frontmatter name not kebab-case: {name!r}")
    dir_name = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if name and dir_name and name != dir_name:
        errors.append(f"frontmatter name {name!r} != directory {dir_name!r}")
    if fm.get("generated-by") and fm["generated-by"] != "forge-agent":
        errors.append(f"generated-by must be 'forge-agent', got {fm['generated-by']!r}")
    # The runtime skill loader parses `allowed-tools` as a comma-separated STRING
    # (gopkg.in/yaml.v3 into a Go string field). A YAML array like ["bash","view"]
    # fails to unmarshal -> the skill is silently treated as malformed and skipped.
    allowed = fm.get("allowed-tools", "")
    if allowed.strip().startswith("["):
        errors.append(
            "allowed-tools must be a comma-separated string (e.g. 'bash, view'), "
            "not a YAML array — the runtime loader parses it as a string and a "
            "sequence makes the whole skill fail to load"
        )
    # YAML reads `key: value` where the value contains ': ' as a nested mapping, so an
    # UNQUOTED description with a colon-space (e.g. 'Triggers: ...') makes the whole
    # frontmatter fail to parse and the skill is silently skipped. Require quoting.
    desc_raw = fm.get("description", "")
    if desc_raw and desc_raw[0] not in "\"'>|" and re.search(r":\s", desc_raw):
        errors.append(
            "description contains a colon followed by whitespace and must be quoted "
            '(wrap the whole value in double quotes, e.g. description: "... Triggers: ...") '
            "— otherwise YAML parses it as a mapping and the skill fails to load"
        )
    category = fm.get("forge-category")
    if category and category not in VALID_CATEGORIES:
        errors.append(
            f"forge-category {category!r} not one of {sorted(VALID_CATEGORIES)}"
        )

    if not re.search(r"^#\s+\S", text, re.MULTILINE):
        errors.append("missing top-level '# <title>' heading")
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"missing required section: {section}")

    if errors:
        print(f"INVALID: {path}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"VALID: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
