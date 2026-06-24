import { Request, Response } from "express";
import { Octokit } from "@octokit/core";

export interface AgentMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface AgentRequest {
  messages: AgentMessage[];
}

/**
 * Parses the incoming Copilot agent request payload.
 */
function parseRequest(req: Request): AgentRequest {
  const body = req.body instanceof Buffer ? req.body.toString("utf-8") : req.body;
  return JSON.parse(body) as AgentRequest;
}

/**
 * Streams a response back to the Copilot chat UI using server-sent events.
 */
async function streamResponse(res: Response, text: string): Promise<void> {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  // Write individual tokens as SSE events
  const words = text.split(" ");
  for (const word of words) {
    const chunk = JSON.stringify({
      choices: [{ delta: { content: `${word} ` }, finish_reason: null }],
    });
    res.write(`data: ${chunk}\n\n`);
  }

  // Signal end of stream
  const done = JSON.stringify({ choices: [{ delta: {}, finish_reason: "stop" }] });
  res.write(`data: ${done}\n\n`);
  res.end();
}

/**
 * Creates a Copilot plugin agent that handles incoming chat messages.
 *
 * Customise the `respond` function below to implement your plugin's logic.
 */
export function createAgent() {
  return {
    async handle(req: Request, res: Response): Promise<void> {
      try {
        // Extract the user's GitHub token forwarded by Copilot
        const tokenHeader = req.headers["x-github-token"];
        const token = Array.isArray(tokenHeader) ? tokenHeader[0] : tokenHeader;

        if (!token) {
          res.status(401).json({ error: "Missing x-github-token header" });
          return;
        }

        const payload = parseRequest(req);
        const lastMessage = payload.messages.at(-1);
        const userInput = lastMessage?.content ?? "";

        // Octokit client authenticated as the user — pass to `respond` for GitHub API calls
        const octokit = new Octokit({ auth: token });

        const reply = await respond(userInput, payload.messages, octokit);
        await streamResponse(res, reply);
      } catch (err) {
        console.error("Agent error:", err);
        res.status(500).json({ error: "Internal agent error" });
      }
    },
  };
}

/**
 * Core response logic for the plugin.
 *
 * Replace this function with your own implementation.
 *
 * @param userInput  The latest message from the user.
 * @param history    The full conversation history.
 * @returns          A string response that will be streamed to the user.
 */
async function respond(userInput: string, _history: AgentMessage[]): Promise<string> {
  // TODO: implement your plugin's response logic here.
  return `Hello from the Copilot PoC plugin! You said: "${userInput}"`;
}
