The output JSONL created by this skill is the **complete desired set** of the repo's repository-scoped memories. It **fully replaces** the existing memories in the DB. Therefore:

- A memory you curate away (duplicate loser, wrong/contradicted fact, mis-scoped preference, low-value noise) is expressed by **omission** — it simply does not appear in the output.
- A merge or reword appears as the **single resulting record**.
- Newly mined repository facts appear as new records (no `id`).
- Order the file by `rank` (most critical first).
