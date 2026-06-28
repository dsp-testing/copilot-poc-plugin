#!/usr/bin/env python3
"""plan_searches.py — turn recon.json into a bounded org code-search battery, and
normalize the results the agent collects back into a service-graph.

The Dreaming sandbox only allows the GitHub MCP server, so this script does NOT call
GitHub. It (a) emits a deterministic *query plan* the agent executes via
`github-mcp-server-search_code`, and (b) in --normalize mode, structures the results
the agent pasted back into a deduped service-graph with explicit assumption tags.

Two modes:
  plan      (default): recon.json + --org -> searches.json (the query battery)
  normalize          : --results results.json -> service-graph.json

The query battery is capped (--max-queries, default 24) so a background run stays cheap.
Every cross-repo claim is tagged assumption=true: code search sees code, not running
deployment state, so the graph is INFERRED and must be human-verified.

Usage:
  plan_searches.py --recon recon.json --org OWNER [--max-queries 24] [--out searches.json]
  plan_searches.py --normalize --results results.json [--out service-graph.json]
"""

from __future__ import annotations

import argparse
import json
import sys

# Deploy-ish path qualifiers keep the battery high-signal in large orgs.
DEPLOY_PATH_QUALIFIERS = [
    "path:**/k8s", "path:**/kubernetes", "path:**/deploy", "path:**/manifests",
    "path:**/helm", "path:**/charts", "path:moda", "path:**/.deployment",
    "filename:values.yaml", "filename:kustomization.yaml", "extension:tf",
    "filename:Dockerfile", "path:**/.github/workflows",
]


def _q(purpose, query, qualifiers=None, note=""):
    full = query if not qualifiers else f"{query} {' '.join(qualifiers)}"
    return {
        "purpose": purpose,
        "query": full,
        "tool": "github-mcp-server-search_code",
        "assumption": True,
        "note": note,
    }


def build_battery(recon: dict, org: str, max_queries: int):
    ids = recon.get("service_identity", [])
    names = [c["name"] for c in ids][:3]  # top-3 identity candidates
    primary = names[0] if names else recon.get("root", "service").split("/")[-1]
    hostnames = recon.get("hostnames", [])[:5]
    org_q = f"org:{org}" if org else ""

    # Build each purpose as its own bucket, then round-robin fill up to the cap so EVERY
    # purpose is represented — trust-context (shared-infra) queries feed the trust
    # boundaries and must not be starved by a large deploy/downstream bucket.
    buckets = {"deploy": [], "downstream": [], "upstream": [], "shared-infra": []}

    # 1) WHERE/HOW DEPLOYED: deploy config in *other* repos referencing this service.
    for nm in names:
        for ql in DEPLOY_PATH_QUALIFIERS[:6]:
            buckets["deploy"].append(_q(
                "deploy", f'"{nm}" {org_q}', [ql],
                f"Where is '{nm}' deployed / referenced in infra?",
            ))

    # 2) DOWNSTREAM: hostnames this service calls -> who/what they belong to.
    for h in hostnames:
        buckets["downstream"].append(_q(
            "downstream", f'"{h}" {org_q}', None,
            f"Which repo owns/defines downstream host '{h}'?",
        ))

    # 3) UPSTREAM: who imports/calls this service (client SDK / hostname references).
    buckets["upstream"].append(_q(
        "upstream", f'"{primary}" {org_q} extension:go extension:ts extension:py', None,
        f"Who imports or references '{primary}' as a client/dependency?",
    ))

    # 4) SHARED INFRA / TRUST CONTEXT: ingress, mesh, secret paths, network policy.
    for kw in ("ingress", "service-mesh OR istio OR linkerd", "vault path", "NetworkPolicy"):
        buckets["shared-infra"].append(_q(
            "shared-infra", f'"{primary}" {kw} {org_q}', None,
            f"Trust-context: '{kw}' positioning of '{primary}'.",
        ))

    # Round-robin across purposes, deduping, until the cap is hit or all buckets drain.
    seen, out = set(), []
    order = ["deploy", "downstream", "upstream", "shared-infra"]
    idx = {k: 0 for k in order}
    while len(out) < max_queries and any(idx[k] < len(buckets[k]) for k in order):
        for k in order:
            if idx[k] >= len(buckets[k]):
                continue
            item = buckets[k][idx[k]]
            idx[k] += 1
            key = (item["purpose"], item["query"])
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= max_queries:
                break

    return {
        "org": org,
        "primary_service": primary,
        "identity_candidates": names,
        "query_count": len(out),
        "queries": out,
        "instructions": (
            "Execute each query with github-mcp-server-search_code. Collect results as a "
            "JSON array of {purpose, query, repo, path, url, snippet}. Then run "
            "plan_searches.py --normalize --results <file> to build service-graph.json. "
            "Treat EVERY edge as an assumption to verify — code search sees code, not "
            "running deployment state."
        ),
    }


def normalize(results: list):
    """Fold raw search results into a deduped, assumption-tagged service graph."""
    nodes, edges = {}, []
    by_purpose = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        repo = r.get("repo", "")
        purpose = r.get("purpose", "unknown")
        by_purpose.setdefault(purpose, set())
        if repo:
            by_purpose[purpose].add(repo)
            nodes.setdefault(repo, {"repo": repo, "roles": set()})
            role = {
                "deploy": "deployer", "downstream": "downstream",
                "upstream": "upstream", "shared-infra": "infra",
            }.get(purpose, "related")
            nodes[repo]["roles"].add(role)
            edges.append({
                "repo": repo, "relation": purpose,
                "path": r.get("path", ""), "url": r.get("url", ""),
                "assumption": True,
            })

    node_list = [
        {"repo": n["repo"], "roles": sorted(n["roles"])}
        for n in nodes.values()
    ]
    # Dedup edges by (repo, relation, path).
    seen, edge_list = set(), []
    for e in edges:
        key = (e["repo"], e["relation"], e["path"])
        if key in seen:
            continue
        seen.add(key)
        edge_list.append(e)

    return {
        "summary": {p: sorted(v) for p, v in by_purpose.items()},
        "nodes": sorted(node_list, key=lambda d: d["repo"]),
        "edges": edge_list,
        "caveat": "INFERRED from code search. Every node/edge is an assumption to verify.",
    }


def _write(obj, out):
    payload = json.dumps(obj, indent=2)
    if out == "-":
        print(payload)
    else:
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print(f"wrote {out}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build / normalize the org code-search battery.")
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("--recon")
    ap.add_argument("--org", default="")
    ap.add_argument("--results")
    ap.add_argument("--max-queries", type=int, default=24)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    if args.normalize:
        if not args.results:
            print("error: --normalize requires --results", file=sys.stderr)
            return 2
        with open(args.results, encoding="utf-8") as f:
            data = json.load(f)
        results = data if isinstance(data, list) else data.get("results", [])
        _write(normalize(results), args.out)
        return 0

    if not args.recon:
        print("error: plan mode requires --recon", file=sys.stderr)
        return 2
    with open(args.recon, encoding="utf-8") as f:
        recon = json.load(f)
    if not args.org:
        print("warn: --org empty; queries will be org-unscoped (noisy)", file=sys.stderr)
    _write(build_battery(recon, args.org, args.max_queries), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
