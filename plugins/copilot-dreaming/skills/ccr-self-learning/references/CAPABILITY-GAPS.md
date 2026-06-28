# Capability gaps: what Dreaming must grow to implement CCR self-learning

The `ccr-self-learning` skill implements the **shape** of the CodeIQ/CCR MVP's learning
extraction ([`CCR_CodeIQ_MVP.md`](https://github.com/github/codeiq-graph/blob/main/docs/CCR_CodeIQ_MVP.md),
milestone **M1 "shadow"**), but the current Dreaming system **cannot yet reach its evidence or
close its loop**. This is the gap analysis. The pipeline reuses the same `miner -> curator`
discipline as `repository-memory-miner` / `repository-memory-curator`; the new parts are the
**evidence connector** and the **learning taxonomy** (suppression / convention / focus).

## Gaps, ranked by criticality

| # | Gap | Why CCR needs it | Dreaming today | What to build |
| --- | --- | --- | --- | --- |
| **G1** | **Evidence connector to CCR's outcome corpus** | The signal lives in CCR's comment-outcome events, CCR-vs-human attribution, thumbs up/down, the **CosmosDB findings store keyed by repo/owner**, and session telemetry (`JOB_API_BASE_URL`). | Evidence sources are local files, GitHub MCP, agent `session_store_sql`, and Slack/WorkIQ — **none is CCR's findings/outcome store**. | A read connector (MCP tool or pre-materialized per-repo export) exposing the outcome-labeled comment corpus. **Headline blocker.** |
| **G2** | **Authenticated egress beyond GitHub MCP** | CCR's corpus + the memory-storage API are first-party Copilot backends needing authenticated calls. | Sandbox firewall blocks `gh api`/`curl`; **only GitHub MCP is allowed**. | An allow-listed, authenticated MCP connector for CCR/Copilot data — or run the miner inside CCR's environment and feed Dreaming the export. G1+G2 are the **critical path**. |
| **G3** | **Org-scoped unit of work** | Learnings are org-level *and* repo-level; the corpus spans many repos. | Runs are single-user/single-repo; the documented workaround ("run as a write-access user in `.github`/`.github-private`") is awkward for org-wide review mining. | An org-scope job model: fan-out per repo + an org-level aggregation/curation pass. (Or repo-scope-only for MVP, deferring org learnings.) |
| **G4** | **Governed write-back path** (memory DB + `.github/skills/`) | M2/M3 apply learnings via the memory-storage API and `.github/skills/`, gated by `IsEnabledForRepoOrOwner` / `CodeReviewBetaFeatures` / `copilot_swe_agent_disallow`, with per-learning enable/disable + "never learn this". | Dreaming **stops at "open an issue"** (`artifact-placement`). No governed DB/skills write, no policy gate, no kill-switch inheritance, no admin inspect/revoke surface. | A governed apply step: policy-gated writes + kill-switch inheritance + a "what CCR learned" inspect/revoke admin view. |
| **G5** | **Trust-boundary-safe materialization (base SHA)** | CCR must materialize skills from the PR **base SHA**, never head, so a PR can't inject a skill into its own review; must honor content-exclusion. | The artifact path has no base-SHA materialization or injection defense for auto-applied artifacts (the human-gated issue only partially mitigates, and only in shadow mode). | Base-SHA materialization + content-exclusion enforcement in the apply step. |
| **G6** | **Effectiveness measurement / ExP holdback / guardrail** | The PRD requires ExP-gated rollout with a holdback and a **launch-blocking bug-catch guardrail**; the Dreaming discussion itself asks "how do we measure effectiveness?" | Dreaming has no artifact-attribution or A/B loop. | An experiment/attribution harness tying an applied learning to downstream acceptance-rate / noise / bug-catch deltas against a holdback. |
| **G7** | **Tenant isolation + content-exclusion in mining** | Strict per-tenant partitioning; no cross-org leakage; honor content-exclusion. | Per-repo runs give natural partitioning, but content-exclusion enforcement during mining of session/outcome data is unestablished. | Enforce content-exclusion + tenant partition in the G1 connector. |

## Not gaps — reuse as strengths (~60% of the plumbing exists)

- Candidate schema with citations / reason / confidence.
- Dedup / threshold / rank machinery (`score_learnings.py` here; `merge-dedup.py` in the memory pipeline).
- Skill generation in `SKILL.md` form.
- Run-dir read-only discipline.
- Scheduling (daily / weekly automations).
- Human-in-the-loop issue surface (`artifact-placement`) — the inspectable, revocable trust surface.

## Critical path & recommendation

- **M1 (shadow) is reachable first** — *iff G1 + G2 are solved* (get the outcome-labeled corpus
  into the sandbox). Everything else in M1 (mine -> candidates -> issue) reuses the PoC and is
  what this skill already implements against an export.
- **M2 / M3 (apply)** require **G4 + G5** (governed, injection-safe write-back).
- **Measurement** requires **G6**.
- **Recommendation:** sequence the Dreaming-platform investment as **G1+G2 -> G4/G5 -> G6**, and
  keep building the skill logic against an export of the corpus now (this skill) so it is ready the
  moment the connector lands. Decide **G3** (org vs repo scope) early — it changes the job topology.
