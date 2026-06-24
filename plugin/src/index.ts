import "dotenv/config";
import express from "express";
import { createAgent } from "./agent";

const app = express();
const port = process.env.PORT || 3000;

// Parse raw body for signature verification
app.use(express.raw({ type: "*/*" }));

// Health check endpoint
app.get("/health", (_req, res) => {
  res.json({ status: "ok", plugin: process.env.PLUGIN_NAME, version: process.env.PLUGIN_VERSION });
});

// Copilot agent endpoint — receives messages from GitHub Copilot
app.post("/", async (req, res) => {
  const agent = createAgent();
  await agent.handle(req, res);
});

app.listen(port, () => {
  console.log(`Copilot plugin server listening on port ${port}`);
});

export default app;
