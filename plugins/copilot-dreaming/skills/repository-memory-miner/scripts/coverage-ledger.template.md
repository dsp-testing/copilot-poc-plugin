<!--
coverage-ledger.template.md — Working ledger the orchestrator maintains DURING a memory-mining run, then finalizes into `<owner>-<repo>.coverage.md` (skill §7.4).

Copy this to the run's working dir, fill the "status" columns as waves complete, and keep it resumable: if a run is interrupted, the next session reads this file and re-dispatches only the streams that are not yet `done`. Stream IDs come from `dispatch.py`'s `$RUN_DIR/dispatch/streams.tsv` and `dispatch-manifest.json` (dispatch.py is installed with the skill, not in the target checkout). Delete this comment in the finalized report.
-->

# Memory-mining coverage & provenance — <owner>/<repo>

- **Run started:** <timestamp> • **Run mode:** top-level multi-wave orchestration
- **Repo scale:** <tracked files> files, <PRs> PRs, <CI runs> runs (fill from probes)
- **Stream plan:** `$SKILL_DIR/scripts/dispatch.py --repo-root "$MAIN" --run-dir "$RUN_DIR"` → <N> streams (<code> CODE, <t> TEST, <d> DATA, <a> ARTIFACT, <e> EXTERNAL); `--exclude` used: <none | globs>; `--max-streams`: <value>

## Staged counts (candidate funnel)

| stage                                          | count |
| ---------------------------------------------- | ----- |
| raw candidates mined (pre-merge)               |       |
| **merged candidates (handed to curator)**      |       |

> Sanity checks: `merged ≤ raw` (dedup only removes; `validate-coverage.py` does not enforce this, but a merged count exceeding raw is a bug). Raw candidates should be **many hundreds** for a non-trivial repo — a few dozen means under-mining (re-dispatch). The miner emits NEW candidates only; existing-memory reconciliation and the final curated count are the curator's job.

## Per-source table (skill §6)

| # | source | status | what was read (counts) | exact searches / queries / IDs |
| --- | --- | --- | --- | --- |
| 1 | Main branch (code) |  | files classified/read | see code ledger below |
| 2 | Cloud agent sessions |  | sessions/turns/files | `session_store_sql` queries |
| 3 | GitHub Actions (CI) |  | runs root-caused | `gh run list/view` invocations |
| 4 | Pull requests |  | PRs + review threads | `gh pr/api` invocations |
| 5 | Issues |  | open + closed sampled | `gh issue list` (issues repos: \_\_\_) |
| 6 | Discussions |  | entries + comments | GraphQL query |
| 7 | Releases / changelog |  | releases read | `gh api .../releases` |
| 8a | Projects v2 |  | projects/items | GraphQL (scope: read:project) |
| 8b | Wiki |  |  | clone attempt |
| 8c | Security advisories |  |  | `gh api .../security-advisories` |
| 9 | Team chat (e.g. Slack) |  | channels/threads | queries + channel IDs |
| 10 | Docs / email / meetings (e.g. WorkIQ/M365) |  | questions asked | each knowledge-source question (e.g. `workiq-ask`) |
| 11 | Telemetry / warehouse |  |  | Kusto/Trino queries (or n/a) |
| 12 | Design docs / ADRs |  | docs parsed | markitdown URLs |

status ∈ `done` / `partial` / `skipped` / `no-access` / `tool-missing`. `no-access` = the connector is present but a resource is permission/scope-gated (name the exact access + how to grant it); `tool-missing` = the connector/capability is absent from this run's environment (nothing to grant — note which capability and that a tool-enabled run would cover it).

For each **connector** stream (sessions, team chat, docs/knowledge, telemetry, design docs), also record in its row or the gaps section: the **wall-clock budget** and any **cutoff** the orchestrator nudged (with the scope left unmined), and any **secondary-tool partial** — a tool the stream needed but did not have surfaced, with exactly what it left uncovered (e.g. "WorkIQ done; doc-parser unsurfaced → 3 cited SharePoint URLs unparsed, handed to `ext:design_docs` follow-up"). A present-primary/absent-secondary stream is `partial`, not `tool-missing`.

## Codebase coverage ledger (one row per CODE / TEST / DATA stream)

| stream id | scope | files | status | covering agent | saturation basis (hi-signal read / skipped-trivial) |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

status ∈ `dispatched` / `done` / `partial` / `skipped`. Every `partial` MUST name the unread high-signal files. A `TEST` stream is a low-priority skim for testing conventions; a `DATA` stream is a `skipped` data directory (record its file count and why it holds no durable memories).

### Excluded / collapsed directories

| directory | how (auto-collapsed `TEST`/`DATA`, or `--exclude <glob>`) | files | reason / note |
| --- | --- | --- | --- |
|  |  |  |  |

> Flag in the Gaps section below any excluded or `skipped` tree that might still hold real code.

## 🚩 Gaps & "Not Done" (most important section)

- **Skipped (with reason):**
- **Partial (what's left + why not closed):**
- **Timed out / returned nothing:**
- **Access / scope / permission failures — `no-access` (exact error + how to grant):**
- **Missing connectors — `tool-missing` (which capability was absent; a tool-enabled run would cover it):**
- **Known artifacts that could not be read:**

> A vague or empty gaps section is a red flag — re-audit before finalizing.
