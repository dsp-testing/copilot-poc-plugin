import { Octokit } from "@octokit/core";
import type { Skill } from "./types";

interface GetIssueParams {
  owner: string;
  repo: string;
  issue_number: number;
}

/**
 * Skill: get_issue
 *
 * Fetches the title, state, body, and labels of a GitHub issue.
 */
export const getIssueSkill: Skill<GetIssueParams> = {
  name: "get_issue",
  description:
    "Fetches details about a specific GitHub issue, including its title, state, body, and labels.",
  parameters: {
    type: "object",
    properties: {
      owner: { type: "string", description: "The owner (user or organisation) of the repository." },
      repo: { type: "string", description: "The repository name." },
      issue_number: { type: "number", description: "The issue number to look up." },
    },
    required: ["owner", "repo", "issue_number"],
  },

  async execute({ owner, repo, issue_number }: GetIssueParams, octokit: Octokit): Promise<string> {
    const response = await octokit.request(
      "GET /repos/{owner}/{repo}/issues/{issue_number}",
      { owner, repo, issue_number },
    );

    const issue = response.data as {
      number: number;
      title: string;
      state: string;
      body: string | null;
      labels: Array<{ name?: string }>;
      html_url: string;
    };

    const labels = issue.labels.map((l) => l.name ?? "").filter(Boolean).join(", ") || "none";

    return [
      `Issue #${issue.number}: ${issue.title}`,
      `State: ${issue.state}`,
      `Labels: ${labels}`,
      `URL: ${issue.html_url}`,
      `Body:\n${issue.body ?? "(no body)"}`,
    ].join("\n");
  },
};
