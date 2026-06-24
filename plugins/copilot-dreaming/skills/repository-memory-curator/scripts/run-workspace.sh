#!/usr/bin/env bash
#
# run-workspace.sh — set up a memory-mining run's workspace and police checkout cleanliness.
#
# A run must never write into the repository it mines (see SKILL.md §1/§7.3 and the
# no-side-effects fragment). This helper owns the two safety-critical, repeatedly-run pieces of
# that contract so they are tested rather than re-derived as inline prose bash each run:
#
#   init  — Resolve and guard the target checkout, create this run's output directory OUTSIDE that
#           checkout (a sibling in its parent dir, or a caller-supplied override), make the per-run
#           scratch/ and checks/ subdirs, and snapshot the target's untracked+ignored baseline so a
#           later subagent leak is detectable. Prints the absolute RUN_DIR on stdout.
#   check — Assert the pinned worktree ($RUN_DIR/main) is pristine AND the invocation checkout
#           gained no new untracked/ignored files since init. Exits non-zero (listing the offending
#           paths) if a subagent — or the orchestrator — leaked files into a checkout.
#
# Why ignored files matter: `git status --porcelain` alone omits gitignored paths, and a target
# repo commonly ignores exactly the names a run produces (`*.json`, `existing.memories*.jsonl`,
# `*.coverage.md`, …), so a stray `gh api … > projects.json` would slip past undetected. We use
# `--ignored=matching --untracked-files=all` (catches a leaked artifact file by pattern, without
# recursing into already-ignored trees like node_modules/) and compare only the new `??`/`!!`
# entries against the baseline, so a user editing a tracked file mid-run does not false-positive.
#
# Usage:
#   run-workspace.sh init  --target <dir> --owner <owner> --repo <repo> [--run-dir <dir>]
#   run-workspace.sh check --run-dir <dir> --target <dir>
#
# Pure git/coreutils; no network, no `gh`. The orchestrator creates the pinned worktree at
# $RUN_DIR/main itself (it needs `gh` to resolve the default branch); this script only sets up and
# polices the workspace.

set -euo pipefail

PROG="$(basename "$0")"

die() {
  printf '%s: error: %s\n' "$PROG" "$*" >&2
  exit 1
}

warn() {
  printf '%s: %s\n' "$PROG" "$*" >&2
}

usage() {
  cat <<'EOF'
run-workspace.sh - set up a memory-mining run's workspace and police checkout cleanliness.

Usage:
  run-workspace.sh init  --target <dir> --owner <owner> --repo <repo> [--run-dir <dir>]
  run-workspace.sh check --run-dir <dir> --target <dir>

init  creates the run's output directory OUTSIDE the target checkout (a sibling in its parent
      directory by default, named memory-mining-<owner>-<repo>-<timestamp>/ and created atomically
      so a same-second collision fails loudly rather than sharing a dir, or the --run-dir
      override), makes its scratch/ and checks/ subdirs, snapshots the target's untracked+ignored
      baseline, and prints the absolute RUN_DIR on stdout.
check asserts $RUN_DIR/main (the pinned worktree) is pristine and the target checkout gained no
      new untracked/ignored files since init; exits non-zero and lists the leaked paths otherwise.

Options:
  --target <dir>   The repository checkout being mined (any path inside its work-tree).
  --owner <owner>  Target repo owner (used for the run-dir name and an origin sanity check).
  --repo <repo>    Target repo name.
  --run-dir <dir>  Override the default run-dir location (should be outside the target checkout; if
                   placed inside, the run dir's own artifacts are excluded from the cleanliness check).
  -h, --help       Show this help and exit.
EOF
}

# Emit the set of untracked (??) and ignored-by-pattern (!!) entries for a checkout, one per line.
# `|| true` neutralizes grep's no-match exit (1); the upstream `git` is safe because every caller
# pre-validates its argument as a work-tree.
status_set() {
  git -C "$1" status --porcelain --ignored=matching --untracked-files=all 2>/dev/null \
    | { grep -E '^(\?\?|!!) ' || true; }
}

# Sanitize an owner/repo component for use in a directory name.
sanitize() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'
}

lc() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

# Fail with a clear message when an option that needs a value is the last token: otherwise
# `shift 2` would fail and, under `set -e`, abort the script silently before the usual validation.
need_val() {
  [ "$#" -ge 2 ] || die "option '$1' requires a value"
}

# Resolve a caller-supplied target path to the top of its git work-tree, failing loudly if it is
# empty or not inside a repository (so a mis-resolved/empty target can never silently land the run
# dir inside the current directory).
resolve_target() {
  local raw="$1" top
  [ -n "$raw" ] || die "--target is required and must be non-empty"
  [ -d "$raw" ] || die "--target '$raw' is not an existing directory"
  top="$(git -C "$raw" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$top" ] || die "--target '$raw' is not inside a git work-tree"
  printf '%s' "$top"
}

cmd_init() {
  local raw_target="" owner="" repo="" run_dir_override=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --target) need_val "$@"; raw_target="$2"; shift 2 ;;
      --owner) need_val "$@"; owner="$2"; shift 2 ;;
      --repo) need_val "$@"; repo="$2"; shift 2 ;;
      --run-dir) need_val "$@"; run_dir_override="$2"; shift 2 ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown init argument: $1" ;;
    esac
  done
  [ -n "$owner" ] || die "--owner is required"
  [ -n "$repo" ] || die "--repo is required"

  local target
  target="$(resolve_target "$raw_target")"

  # Sanity-check that this checkout is actually the requested repo, so a wrong --target (or a
  # session started inside an unrelated repo) is surfaced rather than silently mined.
  local origin_url norm want
  origin_url="$(git -C "$target" config --get remote.origin.url 2>/dev/null || true)"
  if [ -n "$origin_url" ]; then
    norm="$(printf '%s' "$origin_url" | sed -E 's#\.git$##' | sed -E 's#^.*[:/]([^/:]+/[^/]+)$#\1#')"
    want="$owner/$repo"
    if [ "$(lc "$norm")" != "$(lc "$want")" ]; then
      warn "target '$target' origin '$origin_url' does not match requested '$want' — verify --target points at the repo you intend to mine"
    fi
  fi

  local run_dir
  if [ -n "$run_dir_override" ]; then
    mkdir -p "$run_dir_override" || die "cannot create --run-dir '$run_dir_override'"
    run_dir="$(cd "$run_dir_override" && pwd -P)"
  else
    local parent ts safe_owner safe_repo
    parent="$(dirname "$target")"
    if [ ! -d "$parent" ] || [ ! -w "$parent" ]; then
      die "checkout parent '$parent' is not a writable directory"
    fi
    ts="$(date +%Y-%m-%d-%H%M%S)"
    safe_owner="$(sanitize "$owner")"
    safe_repo="$(sanitize "$repo")"
    run_dir="$parent/memory-mining-${safe_owner}-${safe_repo}-${ts}"
    # Plain `mkdir` (not -p) is atomic: it fails if the dir already exists, so two runs that start
    # in the same second never silently share one run dir — the second fails loudly instead.
    if ! mkdir "$run_dir" 2>/dev/null; then
      if [ -e "$run_dir" ]; then
        die "run dir already exists: $run_dir (another run started in the same second — retry)"
      fi
      die "could not create run dir: $run_dir"
    fi
  fi

  mkdir -p "$run_dir/scratch" "$run_dir/checks"
  status_set "$target" > "$run_dir/checks/target-baseline.txt"

  printf '%s\n' "$run_dir"
}

cmd_check() {
  local run_dir="" raw_target=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --run-dir) need_val "$@"; run_dir="$2"; shift 2 ;;
      --target) need_val "$@"; raw_target="$2"; shift 2 ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown check argument: $1" ;;
    esac
  done
  [ -n "$run_dir" ] || die "--run-dir is required"
  [ -d "$run_dir" ] || die "--run-dir '$run_dir' is not an existing directory"

  local target baseline
  target="$(resolve_target "$raw_target")"
  baseline="$run_dir/checks/target-baseline.txt"
  [ -f "$baseline" ] || die "baseline '$baseline' missing — run '$PROG init' first"

  local leak=0 main_checked=0 main="$run_dir/main"

  # Arm 1: the pinned worktree is freshly created, so ANY tracked/untracked/ignored entry is a leak.
  if [ -d "$main" ] && git -C "$main" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    main_checked=1
    local main_dirty
    main_dirty="$(git -C "$main" status --porcelain --ignored=matching --untracked-files=all)"
    if [ -n "$main_dirty" ]; then
      leak=1
      warn "LEAK: pinned worktree '$main' is not pristine:"
      printf '%s\n' "$main_dirty" >&2
    fi
  fi

  # Arm 2: the invocation checkout must not have gained new untracked/ignored files since init.
  local tmp_base tmp_cur new_target
  tmp_base="$(mktemp)"
  tmp_cur="$(mktemp)"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp_base' '$tmp_cur'" EXIT
  sort "$baseline" > "$tmp_base"
  status_set "$target" | sort > "$tmp_cur"
  new_target="$(comm -13 "$tmp_base" "$tmp_cur" || true)"

  # When the run dir is nested inside the checkout (an --run-dir override), its own artifacts are
  # expected, not leaks — drop entries whose path is under the run dir from the comparison.
  if [ -n "$new_target" ]; then
    case "$run_dir/" in
      "$target"/*)
        local rel
        rel="${run_dir#"$target"/}"
        new_target="$(printf '%s\n' "$new_target" \
          | awk -v p="$rel/" '{ path = substr($0, 4); if (index(path, p) == 1) next; print }')"
        ;;
    esac
  fi

  if [ -n "$new_target" ]; then
    leak=1
    warn "LEAK: new untracked/ignored files appeared in the checkout '$target' since init:"
    printf '%s\n' "$new_target" >&2
  fi

  if [ "$leak" -ne 0 ]; then
    warn "STOP: a subagent or the orchestrator leaked files into a checkout. Move the strays under \$RUN_DIR and tighten the offending dispatch before continuing; do not auto-delete from the invocation checkout (it may hold the user's own work)."
    exit 1
  fi
  if [ "$main_checked" -eq 1 ]; then
    printf '%s: clean — %s and %s are pristine\n' "$PROG" "$main" "$target" >&2
  else
    printf '%s: clean — %s is pristine (pinned worktree not present yet)\n' "$PROG" "$target" >&2
  fi
}

main() {
  [ "$#" -gt 0 ] || { usage >&2; die "a subcommand is required (init|check)"; }
  local sub="$1"
  shift
  case "$sub" in
    init) cmd_init "$@" ;;
    check) cmd_check "$@" ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown subcommand: $sub" ;;
  esac
}

main "$@"
