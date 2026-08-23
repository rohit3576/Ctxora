"""Auth: verified-JWT tenant claims with an explicit dev-mode fallback.

AUTH_DISABLED=true (default, compose/local): the request tenant stands and a
warning is logged once per process — dev mode is loud.
AUTH_DISABLED=false: a verified JWT (HS256 via JWT_SECRET, optional iss
check) must carry the configured TENANT_CLAIM; its value overrides the
request tenant. Anything else is 401.
"""

import logging
from dataclasses import dataclass
from typing import Final

import jwt

from config.settings import Settings

_logger = logging.getLogger("ctxora.auth")

_DEV_WARNING: Final = "AUTH_DISABLED=true: request tenant accepted without verification (dev mode)"


class UnauthorizedError(Exception):
    """Missing or invalid credentials."""

    def __init__(self, detail: str) -> None:
        """Describe the auth failure."""
        self.detail: str = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Resolved caller identity."""

    tenant: str
    dev_mode: bool


class TenantAuth:
    """Resolves the effective tenant for one request."""

    def __init__(self, settings: Settings) -> None:
        """Bind settings and remember whether dev mode was already warned."""
        self._settings: Settings = settings
        self._warned: bool = False

    def resolve(self, requested_tenant: str, authorization: str | None = None) -> AuthContext:
        """Return the verified tenant context for one request.

        Raises:
            UnauthorizedError: auth is enabled and the token is unusable.
        """
        if self._settings.auth_disabled:
            if not self._warned:
                _logger.warning(_DEV_WARNING)
                self._warned = True
            return AuthContext(tenant=requested_tenant, dev_mode=True)
        return self._from_bearer(authorization)

    def _from_bearer(self, authorization: str | None) -> AuthContext:
        """Verify the bearer JWT and extract the tenant claim."""
        secret = self._settings.jwt_secret
        if not secret:
            detail = "auth enabled but JWT_SECRET is not configured"
            raise UnauthorizedError(detail)
        raw = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not raw:
            detail = "missing bearer token"
            raise UnauthorizedError(detail)
        try:
            payload: dict[str, object] = dict(
                jwt.decode(
                    raw,
                    secret,
                    algorithms=["HS256"],
                    options={"require": ["exp"]},
                    issuer=self._settings.jwt_issuer,
                )
            )
        except jwt.PyJWTError as exc:
            detail = f"invalid token: {exc}"
            raise UnauthorizedError(detail) from exc
        claim = payload.get(self._settings.tenant_claim)
        if not isinstance(claim, str) or not claim:
            detail = f"token missing tenant claim '{self._settings.tenant_claim}'"
            raise UnauthorizedError(detail)
        return AuthContext(tenant=claim, dev_mode=False)
