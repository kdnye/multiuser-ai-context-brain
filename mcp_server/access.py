"""Who may read and write which silo. Enforced in the server on every call; never left to the prompt.

A silo is an independent wiki stored under its own prefix (`silos/<silo>/`). Silos:
- `users/<oid>`: one per person, owner-only (read and write). `<oid>` is the caller's identity-provider object id
  (a GUID), never the email.
- `shared`: the common reference area. Everyone signed in reads it; only sessions holding the shared-editor role
  write. Reviewing a page before it is published is a process rule; the server enforces only the role.
- role silos: one per name in `BRAIN_ROLE_SILOS` (default `finance,hr,ops`). The role of the same name grants read
  and write on that silo and nothing else.

Roles come from the identity provider (Entra ID app roles in the token's `roles` claim), so access is managed there,
not in this code. Anything not listed here is denied.

Configuration (environment, read on every call so tests and operators see one source of truth):
- `BRAIN_ROLE_SILOS`: comma list of role-gated silo names, each a lowercase slug (`[a-z][a-z0-9-]{1,30}`).
- `BRAIN_SHARED_EDITOR_ROLE`: the role that may write `shared` (default `shared-editor`). It may not be a role silo.
Reserved names (`users`, `shared`, `me`, `index`, `log`, `brain-user`) are refused. `validate_config()` runs at start.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SHARED = "shared"
DEFAULT_ROLE_SILOS = ("finance", "hr", "ops")
DEFAULT_SHARED_EDITOR = "shared-editor"
BASELINE_ROLE = "brain-user"          # every assigned user holds it (deploy/connectors.md), so it can never name a silo
RESERVED = {"users", SHARED, "me", "index", "log", BASELINE_ROLE}
GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GUID_RE = re.compile(GUID)
ROLE_NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")       # always fullmatch: `$` would accept a trailing newline


def role_silos() -> tuple[str, ...]:
    """The configured role-gated silos. A malformed or reserved name is a configuration error, not a silent skip."""
    raw = os.environ.get("BRAIN_ROLE_SILOS")
    names = DEFAULT_ROLE_SILOS if raw is None else tuple(x.strip() for x in raw.split(",") if x.strip())
    editor = _editor_name()
    for n in names:
        if not ROLE_NAME.fullmatch(n) or n in RESERVED or n == editor:
            raise ValueError(f"BRAIN_ROLE_SILOS: {n!r} is not a valid role silo name (reserved, the editor role, or not a slug)")
    return names


def _editor_name() -> str:
    raw = os.environ.get("BRAIN_SHARED_EDITOR_ROLE")
    name = DEFAULT_SHARED_EDITOR if raw is None else raw.strip()
    if not ROLE_NAME.fullmatch(name) or name in RESERVED:
        raise ValueError(f"BRAIN_SHARED_EDITOR_ROLE: {name!r} is not a valid role name")
    return name


def shared_editor_role() -> str:
    """The role that may write `shared`. It must not also be a role silo: a finance member would otherwise edit shared."""
    name = _editor_name()
    if name in role_silos():
        raise ValueError(f"BRAIN_SHARED_EDITOR_ROLE: {name!r} is also a role silo")
    return name


def validate_config() -> None:
    """Fail at startup, not on the first request, if the silo configuration is unsafe."""
    role_silos()
    shared_editor_role()


def silo_pattern() -> re.Pattern:
    alts = "|".join([rf"users/{GUID}", SHARED, *map(re.escape, role_silos())])
    return re.compile(alts)


def normalize_oid(oid: str) -> str:
    """The object id as a lowercase GUID, or ValueError. The home silo is built from it, so it must be exact."""
    o = oid.lower() if isinstance(oid, str) else ""
    if not GUID_RE.fullmatch(o):
        raise ValueError("identity needs an object id (a GUID)")
    return o


@dataclass(frozen=True)
class Identity:
    """The verified caller. Built only from a validated token (or, for local stdio development, the dev env).

    The home silo comes from the object id (`oid`), which is unique per person and never changes, not from the email:
    two addresses can collapse to one name (john.smith / john_smith / john-smith), and a renamed or reused address
    would otherwise strand or inherit a private silo. The email is for display and domain checks only."""

    user_id: str                      # object id (oid), a GUID; normalized to lowercase
    email: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        object.__setattr__(self, "user_id", normalize_oid(self.user_id))

    @property
    def home(self) -> str:
        return f"users/{self.user_id}"


def readable(identity: Identity) -> list[str]:
    """Silos this caller may read, own silo first."""
    return [identity.home, SHARED, *[s for s in role_silos() if s in identity.roles]]


def can_read(identity: Identity, silo: str) -> bool:
    return silo in readable(identity)


def can_write(identity: Identity, silo: str) -> bool:
    if silo == identity.home:
        return True
    if silo == SHARED:
        return shared_editor_role() in identity.roles
    return silo in role_silos() and silo in identity.roles


def resolve(identity: Identity, silo: str | None) -> str:
    """`None` or `me` means the caller's own silo. Anything else must be a known silo name."""
    if silo in (None, "", "me"):
        return identity.home
    # fullmatch, never `$`: `$` also accepts a trailing newline, which would let "shared\n" through.
    if not isinstance(silo, str) or not silo_pattern().fullmatch(silo):
        raise PermissionError(f"unknown silo {silo!r}")
    return silo
