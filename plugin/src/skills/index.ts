import type { Skill } from "./types";
import { getIssueSkill } from "./get-issue";
import { searchReposSkill } from "./search-repos";
import { getUserSkill } from "./get-user";

export type { Skill };

/**
 * All skills registered with this plugin.
 *
 * To add a new skill:
 *  1. Create a new file in this directory that exports a `Skill` object.
 *  2. Import it below and add it to the `skills` array.
 *  The agent loop will automatically pick it up.
 */
export const skills: Skill[] = [
  getIssueSkill,
  searchReposSkill,
  getUserSkill,
];

/** Fast lookup map: skill name → Skill */
export const skillsMap = new Map<string, Skill>(
  skills.map((s) => [s.name, s]),
);
