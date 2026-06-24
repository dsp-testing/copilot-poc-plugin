import { Octokit } from "@octokit/core";
import type { Skill } from "./types";

interface GetUserParams {
  username: string;
}

/**
 * Skill: get_user
 *
 * Retrieves the public profile of a GitHub user or organisation.
 */
export const getUserSkill: Skill<GetUserParams> = {
  name: "get_user",
  description:
    "Retrieves the public GitHub profile for a given username, including their name, bio, company, location, and public repository count.",
  parameters: {
    type: "object",
    properties: {
      username: { type: "string", description: "The GitHub username or organisation login." },
    },
    required: ["username"],
  },

  async execute({ username }: GetUserParams, octokit: Octokit): Promise<string> {
    const response = await octokit.request("GET /users/{username}", { username });

    const user = response.data as {
      login: string;
      name: string | null;
      bio: string | null;
      company: string | null;
      location: string | null;
      public_repos: number;
      followers: number;
      following: number;
      html_url: string;
      type: string;
    };

    return [
      `${user.type}: ${user.login}${user.name ? ` (${user.name})` : ""}`,
      `Bio: ${user.bio ?? "(none)"}`,
      `Company: ${user.company ?? "(none)"}`,
      `Location: ${user.location ?? "(none)"}`,
      `Public repos: ${user.public_repos}`,
      `Followers: ${user.followers}  Following: ${user.following}`,
      `URL: ${user.html_url}`,
    ].join("\n");
  },
};
