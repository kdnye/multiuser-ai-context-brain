---
title: "Connect the brain to Claude or ChatGPT"
type: "how-to"
scope: "work"
source_ids: ["seed:example-2026-11", "docs:deploy-connectors"]
owner: "it"
review_by: "2027-06-30"
audience: ["all"]
---

# Connect the brain to Claude or ChatGPT

Add the brain as a custom connector (an MCP server) in your assistant, then sign in with your work account. Your
account decides which areas you can use, in any assistant.

## Before you start
- IT has assigned your account to the brain's enterprise application, plus any role areas you need.
- Use an AI account and workspace your organisation has approved. Signing in to the brain does not change the AI
  provider's data-handling settings.
- Never paste a client secret into an AI conversation. If the connector needs OAuth client details, IT enters them
  in the product's settings form.

MCP server URL (IT gives you the real one; it ends in `/mcp`):

```text
https://<your-brain-host>/mcp
```

## Claude
1. Open **Settings → Connectors → Add custom connector** (workspace policy may require an administrator).
2. Name it (for example **Context Brain**) and paste the server URL.
3. Choose **Use your own OAuth client**; IT supplies the Claude client id and enters the secret.
4. Click **Add**, sign in with your work account, and approve any multi-factor prompt.
5. In a new conversation, enable the connector in the tool selector and run the check below.

## ChatGPT
In business workspaces only workspace admins and owners can add and publish a custom MCP server. IT adds it once with
a separate OAuth client for ChatGPT, tests it, and publishes it. You then open **Settings → Plugins** (older screens:
**Apps**), choose **Connect**, and sign in. In a conversation, type **@** and choose the brain; the selection applies
to that one message. Product menus change often: check the provider's current documentation if a step is missing.

## Check that it works
Ask:

> Use the brain to list the shared area's sections. Do not create or change anything.

Confirm the assistant actually calls a brain tool and returns a result. An answer from general knowledge does not
prove the connection. Then run the onboarding interview:

> Run my brain onboarding.

## Troubleshooting
| Symptom | What to do |
|---|---|
| "Application not found" during sign-in | IT checks the client id entered in the connector. |
| "Resource does not match scopes" | IT checks the API's identifier URI and the advertised scope (see `deploy/connectors.md`). |
| Redirect URI mismatch | Send IT the error and the callback address shown; each AI client needs its own registered callback. |
| Signed in but access denied | IT checks your assignment and roles; reconnect after a role change to get a fresh token. |
| Tools never load | Send IT the error and which product you used. A successful sign-in alone does not prove tool access. |

Send error text or a screenshot without secret fields. Never send tokens, cookies or browser network exports.

What each area is and who can read it: [[reference/brain-silos-and-who-can-read-them|the silo reference]].
