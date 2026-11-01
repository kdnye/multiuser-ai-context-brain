"""MCP server for the context brain: a multi-user wiki with per-person silos, a shared area and role-gated silos.

Two ways to run it:
- HTTP (MCP_TRANSPORT=http), the hosted service: every request passes the Entra ID gate (entra.py), which attaches
  the verified Identity; every tool acts as that identity through the silo router (router.py). Required env:
  MCP_PUBLIC_URL, ENTRA_TENANT_ID, ENTRA_AUDIENCES, ALLOWED_EMAIL_DOMAINS, BRAIN_BUCKET. The service refuses to start
  if any is missing, and refuses to start if a dev identity is set.
- stdio (default), local development only: the caller is BRAIN_DEV_EMAIL (+ BRAIN_DEV_ROLES), and the
  silos live under BRAIN_ROOT on disk.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import anyio
from mcp.server.fastmcp import Context, FastMCP

from mcp_server.access import Identity, role_silos, validate_config
from mcp_server.router import Brain
from mcp_server.store import CachedStore, FileStore, GcsBackend
from mcp_server.wiki_store import WikiError

BRAIN_NAME = (os.environ.get("BRAIN_NAME") or "Context Brain").strip()


def _instructions() -> str:
    validate_config()
    roles = " / ".join(f"`{r}`" for r in role_silos()) or "(none configured)"
    return f"""\
{BRAIN_NAME}: shared team knowledge plus your own private notes, as linked markdown wikis (Karpathy LLM Wiki
method). Silos: your own (`me`), `shared` (everyone reads; editors write), and role-gated {roles}.
You only ever see silos you are allowed to read; the server enforces it.
- Questions about the organisation, its systems and processes, your work or past decisions: wiki_context(topic)
  first, then wiki_read(path, silo) only for the pages you need. Cite as silo:path. Not covered? Say so; never guess.
- "What was I working on around <date>?": wiki_timeline(date_from, date_to); wiki_list takes date_from/date_to and
  wiki_search takes a YYYY-MM-DD in the query. All act on your own silo unless you name another you may read.
- First session for this user, no `reference/profile` in their silo, or they ask about onboarding / "set up my
  brain" / "how should my brain work for me": wiki_read("how-to/brain-onboarding-interview", silo="shared") and
  follow that script step by step. It writes their profile only after they confirm. If that page is missing, tell
  them the onboarding script is not published yet and to contact the shared-area editor; do not improvise one.
- Write back what lasts to your own silo (wiki_append / wiki_write / wiki_ingest_session). Shared-area changes need
  the shared-editor role; propose them to an editor otherwise.
- Tasks and future needs are records (tasks/, future-needs/): change one with wiki_task_update, never by hand-editing a status.
- "What did I do two weeks ago on Tuesday?": work out the date, then wiki_timeline(date_from, date_to); every page and
  task is dated by its created/date field and its dated history lines.
- Never write credentials, contact details, ID numbers, health, HR or pay data; link to the system of record. One
  exception: on a person page (people/...) in your own silo you may record the person's email, chat handle and phone
  number, plus title, employer and how you know them. Phones are removed everywhere else.
"""


INSTRUCTIONS = _instructions()

AUDIT = logging.getLogger("brain.audit")


def _transport_security():
    pub = (os.environ.get("MCP_PUBLIC_URL") or "").strip().rstrip("/")
    if not pub:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(allowed_hosts=[urlparse(pub).netloc], allowed_origins=[pub])


mcp = FastMCP(os.environ.get("BRAIN_SERVER_NAME") or "context-brain", instructions=INSTRUCTIONS, transport_security=_transport_security())
_brain: Brain | None = None
_brain_lock = threading.Lock()      # tools run in worker threads; build the Brain once


def _audit(event: dict) -> None:
    """One structured line per access decision (who, what, which silo, allowed). No page contents."""
    AUDIT.info("access", extra={"fields": event})


def brain() -> Brain:
    with _brain_lock:
        return _brain_locked()


def _brain_locked() -> Brain:
    global _brain
    if _brain is None:
        if (os.environ.get("BRAIN_STORE") or "file") == "gcs":
            bucket = os.environ["BRAIN_BUCKET"]
            _brain = Brain(lambda silo: CachedStore(GcsBackend(bucket, f"silos/{silo}")), audit=_audit)
        else:
            root = Path(os.environ.get("BRAIN_ROOT", "local-silos"))
            _brain = Brain(lambda silo: FileStore(root / silo), audit=_audit)
    return _brain


def _dev_identity() -> Identity | None:
    email = (os.environ.get("BRAIN_DEV_EMAIL") or "").strip().lower()
    if not email:
        return None
    roles = frozenset(r.strip() for r in (os.environ.get("BRAIN_DEV_ROLES") or "").split(",") if r.strip())
    # Identities are keyed by Entra object id (a GUID). Local development has no Entra, so derive a stable fake one from
    # the dev email (uuid5, fixed namespace): the same dev email always lands in the same local silo.
    oid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"context-brain-dev:{email}"))
    return Identity(user_id=oid, email=email, roles=roles)


def _identity(ctx: Context | None) -> Identity:
    """The verified caller: from the Entra gate over HTTP, or the dev identity under stdio. Otherwise refuse."""
    request = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    if request is not None:
        ident = request.scope.get("state", {}).get("identity")
        if isinstance(ident, Identity):
            return ident
        raise PermissionError("not signed in")
    dev = _dev_identity() if os.environ.get("MCP_TRANSPORT", "stdio") != "http" else None
    if dev is None:
        raise PermissionError("not signed in")
    return dev


async def _run(tool: str, ctx: Context | None, call):
    """Run one tool as the verified caller. Identity is resolved here, on the event loop; the Brain call (blocking
    store and index work) runs in a worker thread so one heavy call cannot freeze every other user. Refusals the
    wiki itself raises are returned as text; anything else is an internal error: the client sees a generic message
    and the log gets only the exception type and tool name (never a message, which can hold page text or paths)."""
    try:
        ident = _identity(ctx)
        return await anyio.to_thread.run_sync(lambda: call(ident))
    except (WikiError, PermissionError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - nothing but a generic error may reach the client
        logging.getLogger("brain.error").error("tool failed", extra={"fields": {"tool": tool, "error_type": type(exc).__name__}})
        return {"error": "internal error"}


@mcp.tool()
async def wiki_context(topic: str, ctx: Context, limit: int = 5) -> dict:
    """Recall what you can see about a topic: best pages across your readable silos, each with neighbours. Call first."""
    return await _run("wiki_context", ctx, lambda who: brain().context(who, topic, limit))


@mcp.tool()
async def wiki_search(query: str, ctx: Context, limit: int = 10) -> dict:
    """Keyword search across the silos you can read; each result names its silo. A date in the query (YYYY-MM-DD) is
    an exact-date filter; with no match the reply says so and gives the date coverage of the silos you can read."""
    return await _run("wiki_search", ctx, lambda who: brain().search(who, query, limit))


@mcp.tool()
async def wiki_read(path: str, ctx: Context, silo: str | None = None) -> dict:
    """Read one page (section/slug) from a silo: `me` (default), `shared`, or a silo your role allows. Returns the
    page's `revision` too (pass it to wiki_write as expected_revision when rewriting)."""
    return await _run("wiki_read", ctx, lambda who: brain().read(who, path, silo))


@mcp.tool()
async def wiki_index(ctx: Context, silo: str | None = None, section: str | None = None, offset: int = 0, limit: int = 300) -> dict:
    """A silo's table of contents, or one section's catalog (paged)."""
    return await _run("wiki_index", ctx, lambda who: brain().index(who, silo, section, offset, limit))


@mcp.tool()
async def wiki_list(ctx: Context, silo: str | None = None, section: str | None = None, scope: str | None = None, limit: int = 200,
              date_from: str | None = None, date_to: str | None = None, sort: str = "path", status: str | None = None,
              not_status: str | None = None) -> dict:
    """List pages in one silo (yours by default), optionally for one section or scope. date_from/date_to (YYYY-MM-DD)
    filter on each page's activity date (session date, else created; never `updated`); sort is path, date_desc,
    date_asc or updated; status / not_status filter records."""
    return await _run("wiki_list", ctx, lambda who: brain().list(who, silo, section, scope, limit, date_from, date_to, sort, status, not_status))


@mcp.tool()
async def wiki_timeline(date_from: str, date_to: str, ctx: Context, silo: str | None = None, scope: str | None = None,
                  section: str | None = None, per_day: int = 15, compact: bool = False) -> dict:
    """What happened in one silo (yours by default) between two dates (YYYY-MM-DD, inclusive), grouped by day:
    sessions, pages created, and dated changes (appended sections, task history). Use it for "what was I working on
    around ...?". For a week or longer use compact=true: each day's sessions in full and everything else as counts
    by kind and page type, so nothing is cut; then call again without compact for one day's detail. Without compact,
    each day lists sessions first, one item per page, at most per_day items (default 15) and 150 in all; every day's
    count is complete and the note says how many were left out. Section narrows it (e.g. "sessions")."""
    return await _run("wiki_timeline", ctx, lambda who: brain().timeline(who, date_from, date_to, silo, scope, section, per_day,
                                                                        compact))


@mcp.tool()
async def wiki_log(ctx: Context, n: int = 10, since: str | None = None, until: str | None = None, silo: str | None = None) -> dict:
    """Log entries (what was ingested or changed) in one silo (yours by default). Default: the last n. With
    since/until (YYYY-MM-DD): the newest n entries in that range, oldest first. Returns {silo, entries, truncated};
    truncated means older entries in the range were left out. Empty since/until count as not given."""
    return await _run("wiki_log", ctx, lambda who: brain().log(who, n, since, until, silo))


@mcp.tool()
async def wiki_links(path: str, ctx: Context, silo: str | None = None) -> dict:
    """A page's links, backlinks and typed relations, within its silo."""
    return await _run("wiki_links", ctx, lambda who: brain().links(who, path, silo))


@mcp.tool()
async def wiki_write(path: str, content: str, summary: str, ctx: Context, silo: str | None = None,
                     expected_revision: str | None = None) -> dict:
    """Create or replace a page in a silo you may write (your own by default). Needs frontmatter; scrubbed.
    When rewriting a page you read, pass its `revision` from wiki_read as expected_revision: if it changed in the
    meantime the write is refused (revision_conflict) instead of silently dropping the other change."""
    return await _run("wiki_write", ctx, lambda who: brain().write(who, path, content, summary, silo, expected_revision))


@mcp.tool()
async def wiki_task_update(path: str, ctx: Context, status: str | None = None, priority: str | None = None, due: str | None = None,
                           horizon: str | None = None, note: str | None = None, silo: str | None = None) -> dict:
    """Change a task (status: Not started, In progress, On Hold, Done; priority: High, Med, Low; due: YYYY-MM-DD) or a
    future need (status: Open, Promoted to Task, Dropped; horizon: Now, Next, Later, Someday) in a silo you may write.
    Adds a dated history line. Project and session pages show the new status the next time they are read."""
    return await _run("wiki_task_update", ctx, lambda who: brain().update_record(who, path, status, priority, due, horizon, note, silo))


@mcp.tool()
async def wiki_append(path: str, text: str, summary: str, ctx: Context, heading: str | None = None, silo: str | None = None) -> dict:
    """Add a dated section to an existing page in a silo you may write."""
    return await _run("wiki_append", ctx, lambda who: brain().append(who, path, text, summary, heading, silo))


@mcp.tool()
async def wiki_ingest_session(payload: dict, ctx: Context) -> dict:
    """Record a working session (canonical JSON) in your own silo; returns pages_to_review to update."""
    return await _run("wiki_ingest_session", ctx, lambda who: brain().ingest_session(who, payload))


class RequestLogMiddleware:
    """One JSON line per request: method, path, status, milliseconds. Never headers, tokens, emails or bodies."""

    def __init__(self, app):
        self.app = app
        self.log = logging.getLogger("brain.request")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start, status = time.monotonic(), [0]

        async def capture(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            level = logging.WARNING if status[0] in (401, 403) else logging.INFO
            self.log.log(level, "request", extra={"fields": {"method": scope.get("method"), "path": scope.get("path"),
                                                              "status": status[0], "ms": round((time.monotonic() - start) * 1000)}})


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"severity": record.levelname, "message": record.getMessage(), **getattr(record, "fields", {})})


def install_json_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def _csv(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


def _http_app():
    """The hosted app behind the Entra gate. Refuses to build if anything is missing or a dev identity is set."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from mcp_server.entra import (
        DISCOVERY,
        EntraGate,
        EntraValidator,
        discovery_document,
    )

    public_url = (os.environ.get("MCP_PUBLIC_URL") or "").strip().rstrip("/")
    tenant = (os.environ.get("ENTRA_TENANT_ID") or "").strip()
    audiences, domains = _csv("ENTRA_AUDIENCES"), _csv("ALLOWED_EMAIL_DOMAINS")
    for name, value in (("MCP_PUBLIC_URL", public_url), ("ENTRA_TENANT_ID", tenant), ("ENTRA_AUDIENCES", audiences),
                        ("ALLOWED_EMAIL_DOMAINS", domains), ("BRAIN_BUCKET", os.environ.get("BRAIN_BUCKET"))):
        if not value:
            raise ValueError(f"{name} is required for MCP_TRANSPORT=http")
    if (os.environ.get("BRAIN_STORE") or "file") != "gcs":
        raise ValueError("MCP_TRANSPORT=http requires BRAIN_STORE=gcs")
    if os.environ.get("BRAIN_DEV_EMAIL"):
        raise ValueError("BRAIN_DEV_EMAIL must never be set on the hosted service")

    # MCP clients send the HTTPS resource URL, so the working scope is <public url>/mcp/brain.access (deploy/connectors.md,
    # "Lessons learned": the api://.../brain.access form fails with AADSTS9010010).
    scopes = _csv("ENTRA_SCOPES") or [f"{public_url}/mcp/brain.access"]

    async def protected_resource(_request):
        return JSONResponse(discovery_document(public_url, tenant, scopes))

    mcp.settings.transport_security = _transport_security()
    install_json_logging()
    app = mcp.streamable_http_app()
    app.router.routes.append(Route(DISCOVERY, protected_resource))
    app.add_middleware(EntraGate, validator=EntraValidator(tenant, audiences, domains),
                       resource_metadata_url=f"{public_url}{DISCOVERY}")
    app.add_middleware(RequestLogMiddleware)
    return app


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        import uvicorn

        uvicorn.run(_http_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), forwarded_allow_ips="*",
                    log_config=None, access_log=False)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
