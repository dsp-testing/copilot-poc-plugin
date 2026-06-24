import { Octokit } from "@octokit/core";
import type { Skill } from "./types";

interface SearchReposParams {
  query: string;
  limit?: number;
}

/**
 * Skill: search_repos
 *
 * Searches GitHub repositories and returns a brief summary of the top results.
 */
export const searchReposSkill: Skill<SearchReposParams> = {
  name: "search_repos",
  description:
    "Searches GitHub for repositories matching a query and returns a summary of the top results.",
  parameters: {
    type: "object",
    properties: {
      query: { type: "string", description: "The search query, e.g. 'copilot extension language:typescript'." },
      limit: { type: "number", description: "Maximum number of results to return (1–10, default 5)." },
    },
    required: ["query"],
  },

  async execute({ query, limit = 5 }: SearchReposParams, octokit: Octokit): Promise<string> {
    const per_page = Math.min(Math.max(1, limit), 10);

    const response = await octokit.request("GET /search/repositories", {
      q: query,
      per_page,
      sort: "stars",
      order: "desc",
    });

    const results = response.data as {
      total_count: number;
      items: Array<{
        full_name: string;
        description: string | null;
        stargazers_count: number;
        html_url: string;
        language: string | null;
      }>;
    };

    if (results.items.length === 0) {
      return `No repositories found for query: "${query}"`;
    }

    const lines = [`Found ${results.total_count.toLocaleString()} repositories. Top ${results.items.length}:`];

    for (const repo of results.items) {
      lines.push(
        `\n• ${repo.full_name} ⭐ ${repo.stargazers_count.toLocaleString()}` +
        (repo.language ? ` [${repo.language}]` : "") +
        `\n  ${repo.description ?? "(no description)"}` +
        `\n  ${repo.html_url}`,
      );
    }

    return lines.join("\n");
  },
};
