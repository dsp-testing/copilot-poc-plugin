Emit one JSON object per line with only these fields:

- `subject` (string): The topic to which this memory relates. 1-2 words. Examples: 'naming conventions', 'testing practices', 'documentation', 'logging', 'authentication', 'sanitization', 'error handling'.
- `fact` (string): The memory text injected into prompts. A clear and short description of a fact about the codebase or a convention used in the codebase. Aim for less than 200 characters. Examples: 'Use JWT for authentication.', 'Follow PEP 257 docstring conventions.', 'Use single quotes for strings in Python.', 'Use Winston for logging.'.
- `citations` (array, **≥1 non-empty**): Sources of this fact, such as file and line numbers in the codebase (e.g., 'path/file.go:123, other/file.ts:45'). If the convention is not explicitly stated in the codebase, you can point at several examples that illustrate it, selecting the most diverse set of examples you can find (e.g. from multiple files or contexts). If the fact is based on user input in an agent session, Slack, or other artifact, quote the exact user input and artifact in the following format: 'User input: "<exact quotation>", Source: "<artifact>"'. If it's based on an external source, cite that source (e.g. 'https://repo-wiki.com/incident-retro259#key-files').
- `reason` (string): A clear and detailed explanation of the reason behind storing this fact. Should be at least 2-3 sentences long, and include which future tasks this fact will be useful for and why it is important to remember it.
- `source` (object): Assembly stamps this authoritatively from the run metadata. Do not invent provenance in subagent output.
- `scope`: Always `"repository"`. Repository-scoped memories apply to this repository's code, design, or conventions, and are relevant to **all repository contributors**. They DO NOT reflect the personal preferences or practices of a single user.
- `rank` (integer): A coarse criticality **tier** 1..5 (1 = most critical); `assemble-final.py` reassigns the final unique 1..N ranks.

This v0 never reads or emits `score`, `votes`, `totalVoteCount`, `id`, `createdAt`, `updatedAt`, `expiresAt`, or other backend-managed fields.
