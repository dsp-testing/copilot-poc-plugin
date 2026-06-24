import { Octokit } from "@octokit/core";

/** JSON Schema subset used to describe skill input parameters. */
export interface ParameterSchema {
  type: "object";
  properties: Record<string, { type: string; description: string; enum?: string[] }>;
  required?: string[];
}

/**
 * A Skill is a discrete, named capability that the Copilot agent can invoke.
 *
 * Each skill exposes:
 *  - A unique `name` used by the LLM when calling the function.
 *  - A human-readable `description` that helps the LLM decide when to use it.
 *  - A `parameters` JSON Schema describing the inputs the skill accepts.
 *  - An `execute` function that runs the skill and returns a plain-text result
 *    which is fed back into the LLM context.
 */
export interface Skill<TParams = Record<string, unknown>> {
  name: string;
  description: string;
  parameters: ParameterSchema;
  execute(params: TParams, octokit: Octokit): Promise<string>;
}
