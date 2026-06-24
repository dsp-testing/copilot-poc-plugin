import { Request, Response } from "express";
import { Octokit } from "@octokit/core";
import { skills, skillsMap } from "./skills/index";

const COPILOT_API = "https://api.githubcopilot.com/chat/completions";

// ── Types ──────────────────────────────────────────────────────────────────

interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

interface Message {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_call_id?: string;
  tool_calls?: ToolCall[];
}

interface CompletionResponse {
  choices: Array<{
    finish_reason: string;
    message: Message;
  }>;
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Emit a single SSE token chunk. */
function writeChunk(res: Response, content: string): void {
  const data = JSON.stringify({ choices: [{ delta: { content }, finish_reason: null }] });
  res.write(`data: ${data}\n\n`);
}

/** Signal end-of-stream. */
function writeDone(res: Response): void {
  const data = JSON.stringify({ choices: [{ delta: {}, finish_reason: "stop" }] });
  res.write(`data: ${data}\n\n`);
  res.end();
}

/** Build the OpenAI-compatible tool definitions from the skills registry. */
function buildTools() {
  return skills.map((skill) => ({
    type: "function" as const,
    function: {
      name: skill.name,
      description: skill.description,
      parameters: skill.parameters,
    },
  }));
}

/** Call the Copilot completions API (non-streaming) and return the parsed response. */
async function callCopilot(token: string, messages: Message[]): Promise<CompletionResponse> {
  const response = await fetch(COPILOT_API, {
    method: "POST",
    headers: {
      // The user's Copilot token is forwarded by GitHub and used to authenticate
      // calls back to the Copilot completions API on their behalf.
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: "gpt-4o", messages, tools: buildTools(), stream: false }),
  });

  if (!response.ok) {
    throw new Error(`Copilot API error: ${response.status} ${response.statusText}`);
  }

  return response.json() as Promise<CompletionResponse>;
}

// ── Agent ──────────────────────────────────────────────────────────────────

/**
 * Main agent request handler.
 *
 * Flow:
 *  1. Parse the incoming Copilot message payload.
 *  2. Call the Copilot completions API with all registered skills as tools.
 *  3. If the model requests a skill (tool_calls), execute it via the skills
 *     registry and append the result as a "tool" message.
 *  4. Loop until the model returns a final text response.
 *  5. Stream the response back to the user as SSE.
 */
async function handleRequest(req: Request, res: Response): Promise<void> {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  try {
    // ── Auth ────────────────────────────────────────────────────────────────
    const tokenHeader = req.headers["x-github-token"];
    const token = Array.isArray(tokenHeader) ? tokenHeader[0] : tokenHeader;

    if (!token) {
      writeChunk(res, "Error: missing x-github-token header.");
      writeDone(res);
      return;
    }

    // ── Parse payload ────────────────────────────────────────────────────────
    const rawBody = req.body instanceof Buffer ? req.body.toString("utf-8") : String(req.body);
    const payload = JSON.parse(rawBody) as { messages: Message[] };
    const messages: Message[] = [...payload.messages];

    // Octokit client authenticated as the calling user
    const octokit = new Octokit({ auth: token });

    // ── Agent loop ────────────────────────────────────────────────────────────
    // Iterate until the model returns a final text response (finish_reason !== "tool_calls").
    const MAX_ITERATIONS = 10;
    for (let i = 0; i < MAX_ITERATIONS; i++) {
      const completion = await callCopilot(token, messages);
      const choice = completion.choices?.[0];

      if (!choice) {
        writeChunk(res, "No response received from Copilot API.");
        break;
      }

      messages.push(choice.message);

      if (choice.finish_reason === "tool_calls" && choice.message.tool_calls) {
        // Execute each requested skill and append results to the conversation
        for (const toolCall of choice.message.tool_calls) {
          const skill = skillsMap.get(toolCall.function.name);
          let result: string;

          if (!skill) {
            result = `Unknown skill: "${toolCall.function.name}"`;
          } else {
            try {
              const params = JSON.parse(toolCall.function.arguments) as Record<string, unknown>;
              result = await skill.execute(params, octokit);
            } catch (err) {
              result = `Error executing skill "${toolCall.function.name}": ${String(err)}`;
            }
          }

          messages.push({ role: "tool", tool_call_id: toolCall.id, content: result });
        }

        // Continue the loop so the model can incorporate the skill results
        continue;
      }

      // Final text response — stream it back token by token
      if (choice.message.content) {
        writeChunk(res, choice.message.content);
      }
      break;
    }
  } catch (err) {
    console.error("Agent error:", err);
    writeChunk(res, "An internal error occurred.");
  }

  writeDone(res);
}

/** Returns an agent object compatible with the Express route in index.ts. */
export function createAgent() {
  return { handle: handleRequest };
}

