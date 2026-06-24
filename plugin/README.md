# Copilot PoC Plugin — Template

A minimal, ready-to-extend [GitHub Copilot Extension](https://docs.github.com/en/copilot/building-copilot-extensions/about-building-copilot-extensions) built with Node.js and TypeScript, featuring a **skills-based architecture** for clean, composable capabilities.

## Project structure

```
plugin/
├── src/
│   ├── index.ts          # Express server & route setup
│   ├── agent.ts          # Agent loop: calls Copilot API, dispatches skills
│   └── skills/
│       ├── types.ts      # Shared Skill interface
│       ├── index.ts      # Skills registry (add new skills here)
│       ├── get-issue.ts  # Skill: fetch a GitHub issue
│       ├── search-repos.ts # Skill: search GitHub repositories
│       └── get-user.ts   # Skill: look up a GitHub user profile
├── .env.example          # Environment variable reference
├── package.json
└── tsconfig.json
```

## How skills work

Each skill is a discrete, named capability described to the Copilot LLM as an OpenAI-compatible function/tool. The agent loop:

1. Sends the user's message to the Copilot completions API together with all registered skills as `tools`.
2. If the model selects a skill (`finish_reason: "tool_calls"`), the agent executes it via the skills registry.
3. The skill result is appended to the conversation and the loop continues until the model returns a final text response.
4. The final response is streamed back to the user as server-sent events.

```
User message
     │
     ▼
Copilot LLM ──tool_calls──► Skills registry ──► execute skill ──► GitHub API
     │                                                                  │
     ◄──────────────────── skill result ──────────────────────────────◄┘
     │
     ▼
Final text response (streamed via SSE)
```

## Built-in skills

| Skill | Description |
|-------|-------------|
| `get_issue` | Fetch title, state, body, and labels of a GitHub issue |
| `search_repos` | Search GitHub repositories and return top results |
| `get_user` | Look up a GitHub user or organisation's public profile |

## Adding a new skill

1. Create a new file in `src/skills/`, e.g. `src/skills/list-prs.ts`:

```typescript
import { Octokit } from "@octokit/core";
import type { Skill } from "./types";

export const listPrsSkill: Skill<{ owner: string; repo: string }> = {
  name: "list_prs",
  description: "Lists open pull requests for a repository.",
  parameters: {
    type: "object",
    properties: {
      owner: { type: "string", description: "Repository owner." },
      repo:  { type: "string", description: "Repository name." },
    },
    required: ["owner", "repo"],
  },
  async execute({ owner, repo }, octokit: Octokit) {
    const { data } = await octokit.request("GET /repos/{owner}/{repo}/pulls", { owner, repo });
    return data.map((pr: { number: number; title: string }) => `#${pr.number} ${pr.title}`).join("\n");
  },
};
```

2. Register it in `src/skills/index.ts`:

```typescript
import { listPrsSkill } from "./list-prs";

export const skills: Skill[] = [
  getIssueSkill,
  searchReposSkill,
  getUserSkill,
  listPrsSkill,   // ← add here
];
```

That's it — the agent loop will automatically offer it to the LLM.

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

The server listens on `http://localhost:3000` by default.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | ✅ | Numeric ID of your GitHub App |
| `GITHUB_APP_PRIVATE_KEY` | ✅ | PEM-encoded private key for the App |
| `GITHUB_WEBHOOK_SECRET` | ✅ | Webhook secret configured in the App |
| `PORT` | ❌ | HTTP port (default: `3000`) |
| `NODE_ENV` | ❌ | `development` or `production` |

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

Update `../marketplace/manifest.json` with the deployed URL (and the skills list if you've added new skills) before submitting the marketplace listing.
