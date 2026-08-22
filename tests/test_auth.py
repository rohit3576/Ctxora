"""Auth tests: dev fallback (warned once), verified JWT, 401 matrix."""

import logging
import time

import jwt
import pytest

from api.auth import TenantAuth, UnauthorizedError
from config.settings import Settings

SECRET = "0123456789abcdef0123456789abcdef"  # noqa: S105 (32-byte RFC 7518 test key)


def make_token(claims: dict[str, object], secret: str = SECRET, exp_offset: int = 600) -> str:
    """Mint one HS256 token."""
    payload = {"exp": int(time.time()) + exp_offset, **claims}
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def enforced() -> TenantAuth:
    return TenantAuth(Settings(auth_disabled=False, jwt_secret=SECRET, tenant_claim="tenant"))


class TestDevMode:
    def test_request_tenant_stands_and_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        auth = TenantAuth(Settings(auth_disabled=True))

        with caplog.at_level(logging.WARNING, logger="datamind.auth"):
            first = auth.resolve("demo")
            second = auth.resolve("demo")

        assert first.tenant == "demo"
        assert first.dev_mode is True
        assert second.dev_mode is True
        warnings = [r for r in caplog.records if "AUTH_DISABLED" in r.message]
        assert len(warnings) == 1


class TestEnforcedMode:
    def test_valid_token_claim_overrides_request(self, enforced: TenantAuth) -> None:
        token = make_token({"tenant": "acme"})

        context = enforced.resolve("spoofed", f"Bearer {token}")

        assert context.tenant == "acme"
        assert context.dev_mode is False

    def test_missing_header_is_401(self, enforced: TenantAuth) -> None:
        with pytest.raises(UnauthorizedError, match="missing bearer"):
            enforced.resolve("demo", None)

    def test_expired_token_is_401(self, enforced: TenantAuth) -> None:
        token = make_token({"tenant": "acme"}, exp_offset=-10)

        with pytest.raises(UnauthorizedError, match="invalid token"):
            enforced.resolve("demo", f"Bearer {token}")

    def test_wrong_signature_is_401(self, enforced: TenantAuth) -> None:
        token = make_token({"tenant": "acme"}, secret="fedcba9876543210fedcba9876543210")  # noqa: S106

        with pytest.raises(UnauthorizedError, match="invalid token"):
            enforced.resolve("demo", f"Bearer {token}")

    def test_missing_claim_is_401(self, enforced: TenantAuth) -> None:
        token = make_token({"sub": "user"})

        with pytest.raises(UnauthorizedError, match="tenant claim"):
            enforced.resolve("demo", f"Bearer {token}")

    def test_no_secret_configured_is_401(self) -> None:
        auth = TenantAuth(Settings(auth_disabled=False, jwt_secret=None))

        with pytest.raises(UnauthorizedError, match="JWT_SECRET"):
            auth.resolve("demo", "Bearer anything")

    def test_issuer_mismatch_is_401(self) -> None:
        auth = TenantAuth(
            Settings(auth_disabled=False, jwt_secret=SECRET, tenant_claim="tenant", jwt_issuer="a")
        )
        token = jwt.encode(
            {"exp": int(time.time()) + 600, "tenant": "acme", "iss": "b"}, SECRET, algorithm="HS256"
        )

        with pytest.raises(UnauthorizedError, match="invalid token"):
            auth.resolve("demo", f"Bearer {token}")
