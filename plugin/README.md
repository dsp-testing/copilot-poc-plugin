# Copilot PoC Plugin — Template

A minimal, ready-to-extend [GitHub Copilot Extension](https://docs.github.com/en/copilot/building-copilot-extensions/about-building-copilot-extensions) built with Node.js and TypeScript.

## Project structure

```
plugin/
├── src/
│   ├── index.ts   # Express server & route setup
│   └── agent.ts   # Copilot agent logic (customise this)
├── .env.example   # Environment variable reference
├── package.json
└── tsconfig.json
```

## Prerequisites

- Node.js ≥ 20
- A registered [GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/about-creating-github-apps) with `Copilot Chat: Read & Write` permissions

## Quick start

```bash
# 1. Install dependencies
npm install

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your GitHub App credentials

# 3. Start the development server
npm run dev
```

The server will listen on `http://localhost:3000` by default.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | ✅ | Numeric ID of your GitHub App |
| `GITHUB_APP_PRIVATE_KEY` | ✅ | PEM-encoded private key for the App |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret configured in the App |
| `PORT` | ❌ | HTTP port (default: `3000`) |
| `NODE_ENV` | ❌ | `development` or `production` |

## Customising the plugin

Edit `src/agent.ts` and replace the `respond` function with your own logic:

```typescript
async function respond(userInput: string, history: AgentMessage[]): Promise<string> {
  // Call an external API, query a database, run business logic, etc.
  return `Your custom response to: "${userInput}"`;
}
```

The agent automatically:
- Parses the Copilot message payload
- Forwards the user's GitHub token so you can call GitHub APIs
- Streams the response back as server-sent events

## Building for production

```bash
npm run build      # Compiles TypeScript → dist/
npm start          # Runs the compiled server
```

## Testing

```bash
npm test
```

## Deployment

Deploy the server to any HTTPS-capable hosting platform (e.g. GitHub Actions + Azure App Service, AWS Lambda, or a container on GHES). Make sure the deployment URL is publicly reachable and set it as the **Webhook URL** in your GitHub App settings.

Update `../marketplace/manifest.json` with the deployed URL before submitting the marketplace listing.
