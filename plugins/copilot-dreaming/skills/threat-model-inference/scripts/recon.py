#!/usr/bin/env python3
"""recon.py — deterministic security recon of a single repo checkout.

Reads ONLY files under --root (no network, no session store), so it is unaffected by
the Dreaming automation sandbox firewall. Extracts the security-relevant shape a STRIDE
threat model needs, plus a best-effort *service identity* used to seed the cross-repo
code-search battery (plan_searches.py).

Output: a JSON object on stdout (or --out) with:
  service_identity : ranked name/image candidates (each with source + confidence)
  languages        : detected languages / package managers (from manifests)
  entrypoints      : exposed surfaces (web routes, EXPOSE ports, handlers)
  auth             : authN/authZ signals (libraries, middleware)
  data_stores      : datastore drivers / ORMs detected
  secrets          : secret-handling signals (managers, env, risky patterns)
  outbound         : outbound/3rd-party signals (http clients, external hosts)
  deploy_artifacts : IaC / deploy files found (Dockerfile, k8s, terraform, CI, moda)
  hostnames        : external hostnames seen in code/config (deduped, capped)

Pure stdlib. Deterministic. Bounded (caps files scanned + bytes per file).

Usage:
  recon.py --root <repo_dir> [--out recon.json] [--max-files 4000] [--max-bytes 200000]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --- Bounds (keep a background run cheap and deterministic) -------------------
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "target", ".next",
    "__pycache__", ".venv", "venv", ".idea", ".vscode", "coverage", ".terraform",
}
TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".cs",
    ".php", ".rs", ".scala", ".clj", ".cljs", ".ex", ".exs", ".yaml", ".yml",
    ".json", ".toml", ".tf", ".hcl", ".env", ".sh", ".md", ".txt", ".cfg",
    ".ini", ".conf", ".properties", ".gradle", ".xml", ".dockerfile",
}

# --- Signal dictionaries -----------------------------------------------------
WEB_ROUTE_PATTERNS = [
    (r"@app\.(?:route|get|post|put|delete|patch)\b", "flask/fastapi route"),
    (r"\brouter\.(?:get|post|put|delete|patch)\s*\(", "express/router route"),
    (r"\bapp\.(?:get|post|put|delete|patch)\s*\(", "express app route"),
    (r"\b(?:r|router|mux)\.(?:HandleFunc|Handle|GET|POST|PUT|DELETE)\b", "go http route"),
    (r"\b(?:get|post|put|patch|delete)\s+['\"]/", "rails/sinatra route"),
    (r"@(?:Get|Post|Put|Delete|Patch|RequestMapping)\b", "spring/nest route"),
    (r"\bgrpc\.|\.proto\b|service\s+\w+\s*\{", "grpc/rpc surface"),
    (r"\b(?:lambda_handler|exports\.handler|func\s+Handler)\b", "serverless handler"),
]
AUTH_PATTERNS = [
    (r"\bjsonwebtoken\b|\bjwt\b|\bJWT\b", "jwt"),
    (r"\boauth2?\b|\bOAuth\b", "oauth"),
    (r"\bpassport\b", "passport"),
    (r"\bdevise\b|\bomniauth\b", "devise/omniauth"),
    (r"\bbcrypt\b|\bargon2\b|\bscrypt\b|\bpbkdf2\b", "password hashing"),
    (r"\bsession\b.*\bsecret\b|\bcookie-session\b|\bexpress-session\b", "session"),
    (r"\bAuthorization:\s*Bearer\b|\bbearer\b", "bearer token"),
    (r"\b(?:authenticate|authorize|require_login|login_required|ensureAuth)\b", "auth middleware"),
    (r"\bSAML\b|\bsaml\b|\bOIDC\b|\boidc\b", "saml/oidc"),
]
DATASTORE_PATTERNS = [
    (r"\bpostgres(?:ql)?\b|\bpg\b|\blib/pq\b|\bpsycopg\b", "postgres"),
    (r"\bmysql\b|\bmariadb\b", "mysql"),
    (r"\bmongo(?:db|ose)?\b", "mongodb"),
    (r"\bredis\b", "redis"),
    (r"\bsequelize\b|\bgorm\b|\bactiverecord\b|\bsqlalchemy\b|\bprisma\b|\bdiesel\b", "orm"),
    (r"\bdynamodb\b|\bcosmos(?:db)?\b|\bbigtable\b|\bspanner\b", "cloud-nosql"),
    (r"\bs3\b|\bblob\s*storage\b|\bgcs\b|\bbucket\b", "object-store"),
    (r"\bkafka\b|\brabbitmq\b|\bsqs\b|\bpubsub\b|\bnats\b", "message-queue"),
    (r"\belasticsearch\b|\bopensearch\b", "search-index"),
]
SECRET_MANAGER_PATTERNS = [
    (r"\bvault\b|\bhashicorp/vault\b", "hashicorp-vault"),
    (r"\bsecretsmanager\b|\bSecretsManager\b|\bssm\b", "aws-secrets"),
    (r"\bkeyvault\b|\bKeyVault\b", "azure-keyvault"),
    (r"\bsecretmanager\b|\bSecret Manager\b", "gcp-secret-manager"),
    (r"\bos\.environ\b|\bprocess\.env\b|\bos\.Getenv\b|\bENV\[", "env-vars"),
]
# Heuristic risky-secret patterns (do NOT print the value; only the fact + location).
RISKY_SECRET_PATTERNS = [
    (r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"][A-Za-z0-9_\-./+]{8,}['\"]", "hardcoded-secret-literal"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "embedded-private-key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "aws-access-key-id"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "github-token"),
]
OUTBOUND_PATTERNS = [
    (r"\baxios\b|\bfetch\s*\(|\brequests\.(?:get|post)\b|\bhttp\.Client\b|\bHttpClient\b|\bnet/http\b|\bRestClient\b", "http-client"),
    (r"\bwebhook\b", "webhook"),
]
HOSTNAME_RE = re.compile(
    r"https?://([a-zA-Z0-9._-]+\.[a-zA-Z]{2,}(?::\d+)?)"
)
DEPLOY_FILES = {
    "dockerfile": "container",
    "docker-compose.yml": "compose",
    "docker-compose.yaml": "compose",
    "procfile": "process-manifest",
    "skaffold.yaml": "k8s-build",
    "chart.yaml": "helm-chart",
    "values.yaml": "helm-values",
    "kustomization.yaml": "kustomize",
}
DEPLOY_DIR_HINTS = [
    (re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$"), "github-actions"),
    (re.compile(r"(?:^|/)moda/"), "moda-deploy"),
    (re.compile(r"(?:^|/)(?:k8s|kubernetes|deploy(?:ments)?|manifests)/.*\.ya?ml$"), "k8s-manifest"),
    (re.compile(r"(?:^|/)(?:helm|charts)/"), "helm"),
    (re.compile(r"\.tf$|\.tfvars$|/terraform/"), "terraform"),
    (re.compile(r"(?:^|/)\.deployment(?:$|/)"), "deployment-config"),
]


def _walk(root: str, max_files: int):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".terraform")]
        for fn in filenames:
            yield os.path.join(dirpath, fn)
            count += 1
            if count >= max_files:
                return


def _read(path: str, max_bytes: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_bytes)
    except OSError:
        return ""


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root)


def _scan_patterns(text: str, patterns):
    hits = set()
    for rx, label in patterns:
        if re.search(rx, text):
            hits.add(label)
    return hits


def detect_service_identity(root: str, max_bytes: int):
    """Best-effort service/image name candidates, ranked by source confidence."""
    cands = []  # (name, source, confidence)

    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            data = json.loads(_read(pkg, max_bytes) or "{}")
            if isinstance(data, dict) and data.get("name"):
                cands.append((str(data["name"]).split("/")[-1], "package.json:name", 0.8))
        except json.JSONDecodeError:
            pass

    gomod = os.path.join(root, "go.mod")
    if os.path.isfile(gomod):
        m = re.search(r"^module\s+(\S+)", _read(gomod, max_bytes), re.MULTILINE)
        if m:
            cands.append((m.group(1).rstrip("/").split("/")[-1], "go.mod:module", 0.8))

    for cm in ("pyproject.toml", "setup.cfg", "setup.py"):
        p = os.path.join(root, cm)
        if os.path.isfile(p):
            m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', _read(p, max_bytes))
            if m:
                cands.append((m.group(1), f"{cm}:name", 0.7))
                break

    # Image names from Dockerfile / compose / k8s.
    for path in _walk(root, 4000):
        base = os.path.basename(path).lower()
        rel = _rel(root, path)
        if base == "dockerfile" or base.endswith(".dockerfile"):
            for m in re.finditer(r"(?im)^\s*(?:FROM\s+\S+\s+AS\s+(\S+)|LABEL\s+.*service[=\s]+['\"]?([\w.-]+))", _read(path, max_bytes)):
                nm = m.group(1) or m.group(2)
                if nm:
                    cands.append((nm, f"{rel}:image-stage", 0.5))
        if base in ("docker-compose.yml", "docker-compose.yaml"):
            for m in re.finditer(r"(?im)image:\s*['\"]?([\w./-]+?)(?::[\w.-]+)?['\"]?\s*$", _read(path, max_bytes)):
                cands.append((m.group(1).split("/")[-1], f"{rel}:image", 0.5))

    # Fallback: repo directory name.
    cands.append((os.path.basename(os.path.abspath(root)), "repo-dir-name", 0.3))

    # Dedup keeping highest confidence per name; sort.
    best = {}
    for name, source, conf in cands:
        name = name.strip()
        if not name:
            continue
        if name not in best or conf > best[name][1]:
            best[name] = (source, conf)
    ranked = sorted(
        ({"name": n, "source": s, "confidence": c} for n, (s, c) in best.items()),
        key=lambda d: d["confidence"], reverse=True,
    )
    return ranked


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic security recon of a repo checkout.")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default="-")
    ap.add_argument("--max-files", type=int, default=4000)
    ap.add_argument("--max-bytes", type=int, default=200_000)
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: --root not a directory: {root}", file=sys.stderr)
        return 2

    languages = set()
    entrypoints, auth, data_stores, secrets, outbound = (set() for _ in range(5))
    risky = []          # list of {pattern, file}
    deploy = []         # list of {file, kind}
    hostnames = set()
    manifest_map = {
        "package.json": "javascript/node", "go.mod": "go",
        "requirements.txt": "python", "pyproject.toml": "python",
        "Gemfile": "ruby", "pom.xml": "java", "build.gradle": "java/kotlin",
        "Cargo.toml": "rust", "composer.json": "php", "mix.exs": "elixir",
    }

    for path in _walk(root, args.max_files):
        rel = _rel(root, path)
        base = os.path.basename(path)
        low = base.lower()
        ext = os.path.splitext(low)[1]

        if base in manifest_map:
            languages.add(manifest_map[base])

        # Deploy artifacts by filename and by path shape.
        if low in DEPLOY_FILES:
            deploy.append({"file": rel, "kind": DEPLOY_FILES[low]})
        for rx, kind in DEPLOY_DIR_HINTS:
            if rx.search(rel.replace(os.sep, "/")):
                deploy.append({"file": rel, "kind": kind})
                break

        if ext not in TEXT_EXT and low not in DEPLOY_FILES and low != "dockerfile":
            continue
        text = _read(path, args.max_bytes)
        if not text:
            continue

        entrypoints |= _scan_patterns(text, WEB_ROUTE_PATTERNS)
        auth |= _scan_patterns(text, AUTH_PATTERNS)
        data_stores |= _scan_patterns(text, DATASTORE_PATTERNS)
        secrets |= _scan_patterns(text, SECRET_MANAGER_PATTERNS)
        outbound |= _scan_patterns(text, OUTBOUND_PATTERNS)
        for rx, label in RISKY_SECRET_PATTERNS:
            if re.search(rx, text):
                risky.append({"pattern": label, "file": rel})
        for m in HOSTNAME_RE.finditer(text):
            h = m.group(1).lower()
            if not h.endswith((".local", ".test", ".example")) and "localhost" not in h:
                hostnames.add(h)

    # Dedup deploy list (path shape + filename can both match).
    seen = set()
    deploy_dedup = []
    for d in deploy:
        key = (d["file"], d["kind"])
        if key not in seen:
            seen.add(key)
            deploy_dedup.append(d)

    out = {
        "root": root,
        "service_identity": detect_service_identity(root, args.max_bytes),
        "languages": sorted(languages),
        "entrypoints": sorted(entrypoints),
        "auth": sorted(auth),
        "data_stores": sorted(data_stores),
        "secrets": {
            "managers": sorted(secrets),
            "risky_findings": risky[:50],
        },
        "outbound": sorted(outbound),
        "deploy_artifacts": deploy_dedup[:100],
        "hostnames": sorted(hostnames)[:50],
    }

    payload = json.dumps(out, indent=2, sort_keys=False)
    if args.out == "-":
        print(payload)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
