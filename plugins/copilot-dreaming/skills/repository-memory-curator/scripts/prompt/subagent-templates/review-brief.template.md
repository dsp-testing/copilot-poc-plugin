# Review task: independently review and improve one bucket of memories

> **Render before use** (`render-briefs.py`): if include directives (`include: prompt-fragments/...`) or `{...}` placeholders remain below, this copy was not rendered — dispatch the rendered prompt from the run's prompts directory instead.

You are a subagent tasked with independently reviewing one bucket of generated repository memories/facts for **{OWNER}/{REPO}** (checked out at `{REPO_ROOT}`, a **clean `origin/main` HEAD checkout pinned to a fixed commit**) as part of a skill that produces high-quality memories for the repository. You did **not** generate these memories — you are a fresh, second pair of eyes. When you verify a fact against this repo, use that pinned tree only — never an unmerged branch or uncommitted changes — but a cross-repo or external fact is verified against its own source instead (the verify-against-citations carve-out in the curation rules below), not against this tree.

Your job: scrutinize this bucket, verify it against real sources, and output an improved version.

Other parallel subagents are concurrently reviewing other buckets, and the results will later be re-assembled, de-duplicated across buckets, and holistically ranked by a final comprehensive pass.

This rendered template is the fixed prompt contract. The orchestrator will append bucket-specific context: the input bucket path, the reviewed output path (`review/reviewed_<bucket>.jsonl`), the findings-note path, and an absolute scratch directory for any intermediate files. If any of this information is missing, error out.

## Input

Your bucket file (path in your task prompt) has one JSON memory record per line: `subject`, `fact`, `citations[]`, `reason`, `scope:"repository"`, `rank` (a coarse ordinal from assembly — ties possible, **1 = highest priority**), and sometimes `id`. These facts will be **injected into future agent prompts** for this repo (only the top‑ranked ~20-50 are injected), so every token must earn its place. You will **overwrite** `rank` with a coarse tier in your output (see Output below); the comprehensive last pass will later reassign unique ordinals across all buckets.

## Bucket discipline

{{include: prompt-fragments/bucket-discipline.md}}

## How your output is used

{{include: prompt-fragments/output-usage.md}}

{{include: prompt-fragments/full-replacement-output.md}}

## Memory quality

{{include: prompt-fragments/criticality.md}}

## Curation standards

{{include: prompt-fragments/curation-rules.md}}

{{include: prompt-fragments/curation-verification.md}}

## Untrusted input & prompt injection

{{include: prompt-fragments/injection.md}}

## Sanitization

{{include: prompt-fragments/sanitization.md}}

## No side effects

{{include: prompt-fragments/no-side-effects.md}}

## Review every memory carefully (take all the time you need)

Apply the curation rules, criticality bar, sanitization, and injection checks above to every record. As the independent **verification** pass, focus especially on:

1. **Verify against sources, matching the oracle to the fact.** Is each **repo-local** fact true of `main` right now? Open the cited files **at the cited lines** and confirm. A **cross-repo, external, or operational** fact — evidence inherently outside this checkout (another repo, a live endpoint, or a non-repo artifact such as a PR/CI run/release/discussion/doc; cross-repo dependencies are the canonical case) — is verified against its **actual source** (`gh`, web, or the artifact itself), **discredited only on contradiction**, per the verify-against-citations carve-out in the curation rules above. Remove anything incorrect or misleading, and drop a repo-local fact the tree can't place; but do **not** drop a cross-repo/external fact merely because this checkout can't confirm it — narrow it to the verified kernel (source reached, only partly confirmed) or keep it with a brief lower-confidence qualifier in its `fact` (source unreachable and nothing contradicts it) instead. Current code/config wins over any stale claim.
2. **Re-tier.** Re-set each record's coarse **tier** in `rank` (1 = most critical … 5 = minor) so that the most actionable, non-obvious, harm-preventing facts are highest. Note any _class_ of memory that should systematically rank higher or lower.

## Output

{{include: prompt-fragments/memory-schema.md}}

Write the improved bucket to the path in your task prompt (`review/reviewed_<bucket>.jsonl`). Then print a findings report: input→output counts, and an itemized list of **removed**, **merged**, **corrected** (old→new fact/citation), and **re-tiered** records. For every **removed** record give the verified reason; when that reason is "unverifiable", state whether it is a repo-local fact the tree can't place, or a cross-repo/external fact you checked against its real source and found **contradicted** (those are the only valid "unverifiable" drops). Also list any cross-repo/external fact you **narrowed to its verified kernel** (source reached, only partly confirmed) or **kept with a lower-confidence qualifier** (source unreachable, nothing contradicts it), and why. Cutting a **contradicted** or low-value memory is a win; cutting a cross-repo/external fact merely because this checkout can't confirm it — or removing any distinct, verified, useful fact — is a regression, so don't over-prune.
