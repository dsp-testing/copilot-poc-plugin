# copilot-poc-plugin

> **⚠️ GitHub Internal Use Only** — See [LICENSE](./LICENSE) for details.

Proof-of-concept [GitHub Copilot Extension](https://docs.github.com/en/copilot/building-copilot-extensions/about-building-copilot-extensions) plugin template and marketplace listing.

## Repository structure

```
copilot-poc-plugin/
├── plugin/           # Copilot Extension plugin template (Node.js / TypeScript)
│   ├── src/
│   │   ├── index.ts  # Express server entry point
│   │   └── agent.ts  # Agent logic — customise this
│   ├── .env.example  # Environment variable reference
│   ├── package.json
│   └── README.md     # Plugin setup & customisation guide
├── marketplace/      # Marketplace manifest & publishing guide
│   ├── manifest.json # Machine-readable plugin listing metadata
│   └── README.md     # How to publish the plugin to the marketplace
├── LICENSE           # GitHub-internal use only
└── README.md         # This file
```

## Getting started

See [`plugin/README.md`](./plugin/README.md) for full setup instructions.

```bash
cd plugin
npm install
cp .env.example .env   # fill in your GitHub App credentials
npm run dev
```

## Publishing to the marketplace

See [`marketplace/README.md`](./marketplace/README.md) for step-by-step publishing instructions.

## License

This project is licensed for **GitHub-internal use only**. See [LICENSE](./LICENSE) for the full terms.
