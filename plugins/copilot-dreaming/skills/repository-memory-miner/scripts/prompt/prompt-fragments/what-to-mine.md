Produce/curate memories that answer these. Each must be **actionable**, **non-obvious**, and **likely to recur** in future tasks:

1. **Biggest gotchas** people and agents get wrong when working in this repo.
2. **Time-sinks** — things agents spend many turns looking for or understanding.
3. **Cross-repo dependencies** — which private repos depend on this one; which private repos this one depends on; what does an agent need to know about those other repos when working here; what changes in this repo require corresponding changes in other repos. Consider using `gh` and web search to answer these questions. These facts are often **not fully verifiable against this repo's checkout** — verify them against the other repo or cited artifact (via `gh`/the live endpoint) and emit them; downstream curation/review keeps a well-sourced cross-repo fact unless its real source contradicts it (the verify-against-citations carve-out), so don't self-censor one just because this tree can't confirm it.
4. **How/where to test** — verified build & test commands, staging environment, test-writing conventions, what to test or verify before committing.
5. **Non-obvious conventions and architecture** that new contributors or agents might get wrong.
6. **Code hot spots** — areas of the codebase that are high-risk, highly complex, frequently changed, require cross-team coordination, require concurrent changes in other repos, or have major implications for efficiency or performance or other production implications.
7. **Common PR-review feedback** — modifications/enhancements reviewers repeatedly ask for.
8. **Incident/revert/churn history** — problems that led to incidents, reverts, or repeated rework and should be remembered and avoided in the future. Exclude problems that can no longer recur following a holistic fix or code hardening.
9. **Most common causes of non-flaky CI failures**.
