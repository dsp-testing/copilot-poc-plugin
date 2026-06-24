# Marketplace Listing

This directory contains the configuration and assets needed to expose the
**Copilot PoC Plugin** in an internal GitHub Copilot Extension marketplace.

## Files

| File | Purpose |
|------|---------|
| `manifest.json` | Machine-readable marketplace manifest describing the plugin's metadata, permissions, and agent endpoint. |

## How to publish

1. **Deploy the plugin server** — follow the instructions in [`../plugin/README.md`](../plugin/README.md) to deploy the agent to a publicly reachable HTTPS URL.

2. **Update the manifest** — replace `<your-deployment-url>` in `manifest.json` with the deployed URL.

3. **Register the GitHub App** — create a GitHub App in your organisation with the following settings:
   - **Webhook URL**: `https://<your-deployment-url>/`
   - **Permissions**: `Copilot Chat → Read & Write`
   - **Callback URL**: *(optional, for OAuth flows)*

4. **Submit for internal listing** — share the `manifest.json` with your internal platform team to have the extension listed in your organisation's Copilot marketplace.

## Manifest schema

The `manifest.json` follows the [Copilot Extensions marketplace manifest schema](https://raw.githubusercontent.com/github/copilot-extensions/main/schema/marketplace-manifest.schema.json).

Key fields:

- **`agent.endpoint`** — the HTTPS URL of your running plugin server.
- **`permissions`** — OAuth scopes the extension needs.
- **`visibility`** — set to `internal` to restrict the listing to GitHub-internal use only.
- **`installation.supported_account_types`** — controls which account types can install the extension.
