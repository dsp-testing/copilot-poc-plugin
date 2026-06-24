You do **not** re-emit records. Write a compact **ordering spec** — a single JSON object — to the spec output path given in your task prompt, expressing every decision as an operation over the stable indices `i` from your input:

```json
{
  "merge": [
    { "members": [5, 88], "keep": 5 },
    {
      "members": [3, 12],
      "subject": "<topic, 1-2 words>",
      "fact": "<new combined fact>",
      "reason": "<why it matters>"
    }
  ],
  "reword": [
    {
      "i": 9,
      "subject": "<topic, 1-2 words>",
      "fact": "<tightened or corrected fact>"
    }
  ],
  "drop": [7, 41],
  "order": [5, 3, 9, 2]
}
```

- **`merge`** — consolidate ≥2 records (by index) into one, **anywhere within the list you were given** (full semantic dedup). There are two kinds of merges:
  - **Duplicates** (they say the same thing): set **`keep`** to the index of the **best** member — the clearest and most complete wording. That member's `subject`/`fact`/`reason` are kept **verbatim**; the other members' **citations are unioned in**, and the others are dropped.
  - **Closely related** (distinct facts, but one tighter `fact` covers them better than keeping both): write a **new** `subject`, `fact`, and `reason` (no `keep`). This **replaces** every member with one new memory, with their citations unioned. Only consolidate when the combined record loses no actionable specificity; otherwise keep them separate.
- Give each merge **exactly one** of `keep` (duplicates) **or** `subject` + `fact` + `reason` (closely related). New fields must meet the memory schema: **`subject`** — the topic, in 1–2 words; **`fact`** — the memory text injected into prompts, a clear, self-contained, accurate statement (aim under 200 characters); **`reason`** — 2–3 sentences on why the fact is valuable and which future tasks it helps. Sanitize all three per the rules below. For a **duplicate** (`keep`) merge you author nothing — the kept member's fields are reused. Never author `citations` or `rank` — assembly derives them.
- **`reword`** — rewrite a **single** record's `subject` and `fact` in place (by its index `i`), when its wording can be tightened or a stale/misleading clause excised. Same rules apply for the fields you author as the closely-related merge case. You author **only `subject` and `fact`** — not `reason`: a future agent sees only `fact`, so `reason` is unseen curation metadata and the original record's `reason` is carried over unchanged. A reword must therefore **actually change the `fact`** — a byte-identical `fact` is rejected. Reword only when it materially improves clarity, accuracy, or signal — leave an already-clear record alone.
