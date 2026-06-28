#!/usr/bin/env python3
"""score_risks.py — validate + score the agent-authored STRIDE threat inventory.

The agent enumerates threats (one per high-signal DFD element/flow) into threats.json;
this script enforces a consistent, deterministic severity so two runs of the same model
can't disagree on the rating. It:

  1. validates each threat record's required fields and enums,
  2. computes severity from likelihood x impact (3x3 matrix),
  3. amplifies one level when the threat crosses a trust boundary,
  4. sorts by severity and writes back a normalized threats.json,
  5. renders a Markdown risk table (for THREAT-MODEL.md) to --table.

STRIDE = Spoofing, Tampering, Repudiation, Information disclosure, Denial of service,
Elevation of privilege. We reuse CWE/OWASP identifiers (not the source skill's KB).

Threat record schema (agent-authored):
  {
    "id": "T-001",
    "element": "DFD element or flow this applies to",
    "stride": "Spoofing|Tampering|Repudiation|InformationDisclosure|DenialOfService|ElevationOfPrivilege",
    "threat": "one-line description of the threat",
    "cwe": "CWE-89",                       # optional but recommended
    "owasp": "A03:2021-Injection",         # optional
    "likelihood": "low|medium|high",
    "impact": "low|medium|high",
    "crosses_trust_boundary": true,        # optional, default false
    "mitigation": "concrete mitigation"    # required for High/Critical (enforced as warning)
  }

Exit 0 = valid (warnings allowed); 1 = hard validation failure; 2 = usage error.

Usage:
  score_risks.py --threats threats.json [--out threats.json] [--table risk-table.md]
"""

from __future__ import annotations

import argparse
import json
import sys

STRIDE = {
    "Spoofing", "Tampering", "Repudiation",
    "InformationDisclosure", "DenialOfService", "ElevationOfPrivilege",
}
LEVELS = {"low": 1, "medium": 2, "high": 3}
# likelihood x impact -> base severity.
SEVERITY_MATRIX = {
    (1, 1): "Low", (1, 2): "Low", (1, 3): "Medium",
    (2, 1): "Low", (2, 2): "Medium", (2, 3): "High",
    (3, 1): "Medium", (3, 2): "High", (3, 3): "Critical",
}
SEV_ORDER = {"Critical": 4, "High": 3, "Medium": 2, "Low": 1}
SEV_UP = {"Low": "Medium", "Medium": "High", "High": "Critical", "Critical": "Critical"}


def score_one(t: dict, errors: list, warnings: list, idx: int):
    tid = t.get("id") or f"(index {idx})"
    for key in ("id", "element", "stride", "threat", "likelihood", "impact"):
        if not t.get(key):
            errors.append(f"{tid}: missing required field '{key}'")
    stride = t.get("stride", "")
    if stride and stride not in STRIDE:
        errors.append(f"{tid}: invalid stride {stride!r}; expected one of {sorted(STRIDE)}")
    lk = str(t.get("likelihood", "")).lower()
    im = str(t.get("impact", "")).lower()
    if lk and lk not in LEVELS:
        errors.append(f"{tid}: likelihood must be low|medium|high, got {lk!r}")
    if im and im not in LEVELS:
        errors.append(f"{tid}: impact must be low|medium|high, got {im!r}")
    if lk in LEVELS and im in LEVELS:
        sev = SEVERITY_MATRIX[(LEVELS[lk], LEVELS[im])]
        if t.get("crosses_trust_boundary"):
            sev = SEV_UP[sev]
        t["severity"] = sev
        if sev in ("High", "Critical") and not t.get("mitigation"):
            warnings.append(f"{tid}: {sev} threat has no mitigation")
    return t


def render_table(threats: list) -> str:
    rows = [
        "| ID | Severity | STRIDE | Element | CWE | Likelihood | Impact | XBoundary | Mitigation |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in threats:
        rows.append(
            f"| {t.get('id','')} | {t.get('severity','?')} | {t.get('stride','')} | "
            f"{t.get('element','')} | {t.get('cwe','')} | {t.get('likelihood','')} | "
            f"{t.get('impact','')} | {'yes' if t.get('crosses_trust_boundary') else 'no'} | "
            f"{(t.get('mitigation','') or '').replace(chr(10),' ')} |"
        )
    return "\n".join(rows) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate + score the STRIDE threat inventory.")
    ap.add_argument("--threats", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--table", default="")
    args = ap.parse_args()

    try:
        with open(args.threats, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read --threats: {e}", file=sys.stderr)
        return 2

    threats = data if isinstance(data, list) else data.get("threats", [])
    if not isinstance(threats, list) or not threats:
        print("error: no threats found (expect a JSON array or {\"threats\": [...]})", file=sys.stderr)
        return 1

    errors, warnings = [], []
    ids = set()
    for i, t in enumerate(threats):
        if not isinstance(t, dict):
            errors.append(f"(index {i}): not an object")
            continue
        score_one(t, errors, warnings, i)
        tid = t.get("id")
        if tid in ids:
            errors.append(f"duplicate id: {tid}")
        ids.add(tid)

    threats.sort(key=lambda t: SEV_ORDER.get(t.get("severity", "Low"), 0), reverse=True)

    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if errors:
        print(f"INVALID: {args.threats}", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    counts = {}
    for t in threats:
        counts[t["severity"]] = counts.get(t["severity"], 0) + 1
    print(f"VALID: {len(threats)} threats — " +
          ", ".join(f"{k}:{counts.get(k,0)}" for k in ("Critical", "High", "Medium", "Low")))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"threats": threats, "severity_counts": counts}, f, indent=2)
            f.write("\n")
        print(f"wrote {args.out}", file=sys.stderr)
    if args.table:
        with open(args.table, "w", encoding="utf-8") as f:
            f.write(render_table(threats))
        print(f"wrote {args.table}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
