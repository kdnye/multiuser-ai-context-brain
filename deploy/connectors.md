# Entra ID and AI connector setup (administrators)

The brain is an OAuth 2.0 protected resource (RFC 9728). AI clients (Claude, ChatGPT) discover the authorization
server from `/.well-known/oauth-protected-resource`, sign the user in with Microsoft Entra ID, and send the resulting
v2 access token as a bearer token. The server validates it (`mcp_server/entra.py`) and maps the user's app roles to
silos (`mcp_server/access.py`). Below, `<service-url>` is the host part of `$URL` from deploy/README.md step 1.

Status of this guide: the Claude flow below was verified end to end in a production tenant (2026-10). The ChatGPT
flow is a checklist from OpenAI's documentation and has **not** been verified against this server yet.

## 1. The API registration (one per deployment)
| Setting | Value |
|---|---|
| Supported accounts | Single tenant |
| Application ID URIs (top-level `identifierUris`) | `api://<API_CLIENT_ID>` **and** the exact HTTPS MCP URL, `https://<service-url>/mcp` |
| Expose an API → scope | `brain.access`, admin consent only |
| Manifest | `api.requestedAccessTokenVersion = 2` |
| App roles | `brain-user` (baseline), `shared-editor`, and one per role silo (default `finance`, `hr`, `ops`); allowed member types **Users/Groups** only |
| Enterprise application | **Assignment required = Yes**; assign users or groups their roles |

Cloud Run env: `ENTRA_TENANT_ID=<TENANT>`, `ENTRA_AUDIENCES=<API_CLIENT_ID>`,
`ENTRA_SCOPES=https://<service-url>/mcp/brain.access`, `ALLOWED_EMAIL_DOMAINS=<DOMAIN>`.

## 2. The Claude OAuth client (verified)
- A separate single-tenant registration, e.g. "Context Brain – Claude".
- Web redirect URI: `https://claude.ai/api/mcp/auth_callback`.
- API permission: the API's delegated `brain.access`; grant admin consent.
- A client secret with a tracked expiry. Enter its **Value** (not the Secret ID) directly into Claude's
  "Use your own OAuth client" form. Never put it in git, chat, build substitutions or logs.
- Where the Claude workspace supports administrator-managed connectors, publish it there so staff only connect
  their own identity.

## 3. The ChatGPT OAuth client (not yet verified)
From OpenAI's documentation (read 2026-10): only workspace admins/owners can add a custom MCP server in business
workspaces; ChatGPT needs a predefined client (Entra offers neither CIMD nor DCR); refresh needs `offline_access`;
ChatGPT freezes the tool list at publish time, so republish after any tool change.
1. Create a separate single-tenant registration, e.g. "Context Brain – ChatGPT".
2. Start creating the plugin in ChatGPT to see its redirect URI, and register that exact value. Do not reuse
   Claude's callback and do not guess the URI.
3. Delegated permissions: `brain.access`, `openid`, `profile`, `email`, `offline_access`; admin consent for this
   client (Claude's consent does not carry over).
4. Secret with a tracked expiry, entered only in ChatGPT's OAuth form.
5. Test privately (sign-in, tool list, a read, an unauthorized-silo refusal) before publishing to the workspace.
   Record what worked in this file through a PR.

## Lessons learned (each one was a failed sign-in)
- **`AADSTS9010010` (resource does not match scopes):** MCP clients send the HTTPS resource URL, so the API needs
  that URL as an identifier URI and `ENTRA_SCOPES` must be the HTTPS form (`https://<service-url>/mcp/brain.access`).
  The `api://.../brain.access` form fails.
- **`AADSTS700016` (application not found):** a mistyped client id. Copy ids, never retype them.
- `identifierUris` inside `servicePrincipalLockConfiguration` is a Boolean, not the URI list. Edit the top-level one.
- Do not give the Claude or ChatGPT client registrations the API's resource URI.
- App roles meant for people must allow **Users/Groups**, not Applications.
- The server does not require the `brain-user` role claim; assignment-required on the enterprise app is what keeps
  unassigned people out. Prove it with an unassigned test account.
- After a role change, the user reconnects to get a fresh token.
- **The token's `scp` must contain the scope name from `ENTRA_SCOPES`** (the part after the last `/`, `brain.access`
  by default). If you named the scope something else, set `ENTRA_SCOPES` to match, or every sign-in is refused. This
  is also what keeps two connectors on one API registration apart: they share the audience, so give each server its
  own scope name and its own `ENTRA_SCOPES`. App-only (client-credentials) tokens carry no `scp` and are refused.
  After upgrading, connect once and call a tool straight away; there is no bypass setting, so roll back to the
  previous revision if a sign-in that worked before now gets a 401.

## References
- MCP authorization spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- OpenAI, custom MCP servers in ChatGPT: https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
- Microsoft identifier URI rules: https://learn.microsoft.com/en-us/entra/identity-platform/identifier-uri-restrictions
