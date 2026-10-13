"""Server-provisioned bearer identities and resource authorization."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str

    def require(self, *roles: str) -> None:
        if self.role not in roles and self.role != "admin":
            raise HTTPException(403, "role is not permitted")

    def access(self, owner_id: str | None) -> None:
        if self.role == "initiator" and owner_id != self.subject:
            raise HTTPException(403, "resource belongs to another user")
        if owner_id is None and self.role != "admin":
            raise HTTPException(403, "legacy resource requires administrator")


async def authenticate(request: Request) -> Principal:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        for configured, identity in request.app.state.settings.api_tokens.items():
            if secrets.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
                subject, role = identity.get("subject"), identity.get("role")
                if subject and role in {"initiator", "reviewer", "approver", "admin"}:
                    return Principal(subject, role)
    raise HTTPException(401, "valid bearer credential required", headers={"WWW-Authenticate": "Bearer"})
