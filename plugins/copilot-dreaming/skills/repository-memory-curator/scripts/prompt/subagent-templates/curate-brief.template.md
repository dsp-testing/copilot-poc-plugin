# Curation task: curate one bucket of memories

> **Render before use** (`render-briefs.py`): if include directives (`include: prompt-fragments/...`) or `{...}` placeholders remain below, this copy was not rendered — dispatch the rendered prompt from the run's prompts directory instead.

You are a subagent tasked with curating one bucket of candidate memories for **{OWNER}/{REPO}** (checked out at `{REPO_ROOT}`) as part of a skill that produces high-quality memories for the repository.

Other parallel subagents are concurrently curating other candidate buckets, and the buckets will be merged holistically in a subsequent step.

This rendered template is the fixed prompt contract. The orchestrator will append bucket-specific context: bucket file path, output curated JSONL path, findings-note path, an absolute scratch directory for any intermediate files, and any run-level counts. If any of this information is missing, error out.

## Bucket discipline

{{include: prompt-fragments/bucket-discipline.md}}

## How your output is used

{{include: prompt-fragments/output-usage.md}}

{{include: prompt-fragments/full-replacement-output.md}}

## Curation rules

{{include: prompt-fragments/curation-rules.md}}

{{include: prompt-fragments/curation-verification.md}}

## Memory quality

{{include: prompt-fragments/criticality.md}}

## Untrusted input & prompt injection

{{include: prompt-fragments/injection.md}}

## Sanitization

{{include: prompt-fragments/sanitization.md}}

## No side effects

{{include: prompt-fragments/no-side-effects.md}}

## Output

{{include: prompt-fragments/memory-schema.md}}

Write the records to the assigned `curated_<bucket>.jsonl` path and overwrite any prior `rank`/ordinal from the input bucket. Also write the assigned findings note with input→output counts, merges, rewords, ids kept, unverifiable citations, and the reason for each dropped memory — and when a drop reason is "unverifiable", say whether it is a repo-local fact the tree can't place, or a cross-repo/external fact contradicted by its real source (the only valid "unverifiable" drops), plus any cross-repo/external fact you narrowed to a verified kernel or kept with a lower-confidence qualifier, so the orchestrator's drop-log audit can scrutinize it.
