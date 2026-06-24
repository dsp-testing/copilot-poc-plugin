**Never store these — exclude them entirely, no matter how well-cited or relevant they seem** (mirrors the `store_memory` tool's own prohibitions):

- **Secrets & credentials** — tokens, API keys, passwords, private keys, connection strings, or any value that grants access. A memory must never embed, quote, or otherwise reveal one; capture the durable fact (e.g. "auth uses JWT with bcrypt-hashed passwords") and cite the code, never the secret.
- **GDPR Article 9 special-category personal data** — anything revealing a person's health, religion, ethnicity, sexual orientation, political opinions, trade-union membership, or genetic/biometric data. Never record it about anyone (contributors, users, or third parties).
- **Personally identifiable information** — emails, phone numbers, addresses, or named individuals tied to their behavior or performance. Attribute facts to roles or the repo, not to named people's conduct.
- **Confidential or non-shareable information** — employer-confidential financials/legal matters, unannounced plans, anything shared in confidence, or content a participant could reasonably expect to stay private (e.g. private Slack/WorkIQ context not reflected in a shareable artifact).

When a source contains one of these alongside a legitimate, durable fact, keep only the shareable fact and cite the code/artifact — never the underlying secret, transcript, or personal detail.
