import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
import httpx

from multiclaw.cli import alembic_config
from multiclaw.config import DatabaseSettings, Settings
from multiclaw.config.settings import SecretSettings
from multiclaw.secrets.envelope import EnvelopeFields, SecretEnvelopeService
from multiclaw.secrets.keyring import DeploymentKeyring
from multiclaw.secrets.resolver import SecretResolver
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'secret-validation.db'}"


def _keyring_payload() -> str:
    return base64.b64encode(
        json.dumps(
            {
                "active_key_version": 3,
                "keys": {
                    "3": base64.b64encode(bytes(range(32))).decode("ascii"),
                },
            }
        ).encode("utf-8")
    ).decode("ascii")


async def _create_database(tmp_path: Path) -> Database:
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=database_url))


class _TrackingResolver(SecretResolver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_resolved = None

    async def resolve(self, *args, **kwargs):
        resolved = await super().resolve(*args, **kwargs)
        self.last_resolved = resolved
        return resolved


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return dict(self._body)


class _FakeClient:
    def __init__(self, *, response=None, error: BaseException | None = None, capture=None):
        self._response = response
        self._error = error
        self._capture = capture if capture is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, *, headers):
        self._capture.append({"url": url, "headers": dict(headers)})
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClientFactoryError:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def secret_validation_db(tmp_path: Path):
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


async def _seed_context(database: Database) -> TenantContext:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace("secret-validation@example.com")
        assert user.default_workspace_id is not None
        return TenantContext(user.id, user.default_workspace_id)


async def _seed_secret(
    database: Database,
    context: TenantContext,
    *,
    provider_name: str,
    secret_name: str = "api_key",
    plaintext: str = "secret-canary-value",
) -> None:
    keyring = DeploymentKeyring.load(SecretSettings(), environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()})
    envelope = SecretEnvelopeService(keyring)
    secret_id = str(uuid4())
    async with TenantUnitOfWork(database, context) as uow:
        await uow.secrets.put_encrypted(
            secret_id=secret_id,
            provider_kind="llm",
            provider_name=provider_name,
            secret_name=secret_name,
            record=envelope.encrypt(
                plaintext.encode("utf-8"),
                EnvelopeFields(
                    tenant_id=context.tenant_id,
                    workspace_id=None,
                    secret_id=secret_id,
                    provider_kind="llm",
                    provider_name=provider_name,
                    secret_name=secret_name,
                ),
            ),
        )


def _resolver(database: Database, *, platform_lookup=None) -> _TrackingResolver:
    keyring = DeploymentKeyring.load(SecretSettings(), environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()})
    return _TrackingResolver(
        database=database,
        settings=SecretSettings(allow_platform_fallback=True),
        keyring=keyring,
        platform_lookup=platform_lookup,
    )


@pytest.mark.asyncio
async def test_secret_credential_tester_openai_success_uses_models_endpoint_and_zeroizes(secret_validation_db: Database):
    from multiclaw.secrets.validation import SecretCredentialTester

    context = await _seed_context(secret_validation_db)
    await _seed_secret(secret_validation_db, context, provider_name="openai", plaintext="openai-secret")
    resolver = _resolver(secret_validation_db)
    captured: list[dict[str, object]] = []
    settings = Settings(
        _config_file="/nonexistent",
        llm={"providers": {"openai": {"base_url": "https://api.openai.example/v1"}}},
    )
    tester = SecretCredentialTester(
        resolver=resolver,
        settings=settings,
        client_factory=lambda **kwargs: _FakeClient(
            response=_FakeResponse(200, {"data": []}),
            capture=captured,
        ),
    )

    result = await tester.validate(context, "llm", "openai", "api_key")

    assert result.ok is True
    assert captured == [
        {
            "url": "https://api.openai.example/v1/models",
            "headers": {
                "Authorization": "Bearer openai-secret",
                "Accept": "application/json",
            },
        }
    ]
    assert resolver.last_resolved.secret_bytes.is_zeroized()


@pytest.mark.asyncio
async def test_secret_credential_tester_invalid_credentials_and_no_platform_fallback(secret_validation_db: Database):
    from multiclaw.secrets.validation import InvalidSecretCredentialsError, SecretCredentialTester

    context = await _seed_context(secret_validation_db)
    await _seed_secret(secret_validation_db, context, provider_name="deepseek", plaintext="revoked-secret")
    platform_calls: list[tuple[str, str, str]] = []
    resolver = _resolver(
        secret_validation_db,
        platform_lookup=lambda kind, name, secret: platform_calls.append((kind, name, secret)) or "platform-secret",
    )
    settings = Settings(
        _config_file="/nonexistent",
        llm={"providers": {"deepseek": {"base_url": "https://api.deepseek.example/v1"}}},
    )
    tester = SecretCredentialTester(
        resolver=resolver,
        settings=settings,
        client_factory=lambda **kwargs: _FakeClient(response=_FakeResponse(401, {"error": "bad key"})),
    )

    with pytest.raises(InvalidSecretCredentialsError):
        await tester.validate(context, "llm", "deepseek", "api_key")

    assert platform_calls == []
    assert resolver.last_resolved.secret_bytes.is_zeroized()


@pytest.mark.asyncio
async def test_secret_credential_tester_maps_timeouts_and_5xx_without_leaking(secret_validation_db: Database):
    from multiclaw.secrets.validation import SecretCredentialServiceUnavailableError, SecretCredentialTester

    context = await _seed_context(secret_validation_db)
    await _seed_secret(secret_validation_db, context, provider_name="anthropic", plaintext="anthropic-secret")
    resolver = _resolver(secret_validation_db)
    settings = Settings(
        _config_file="/nonexistent",
        llm={"providers": {"anthropic": {"base_url": "https://api.anthropic.example"}}},
    )

    timeout_tester = SecretCredentialTester(
        resolver=resolver,
        settings=settings,
        client_factory=lambda **kwargs: _FakeClient(error=asyncio.TimeoutError("timed out /tmp/secret")),
    )
    with pytest.raises(SecretCredentialServiceUnavailableError):
        await timeout_tester.validate(context, "llm", "anthropic", "api_key")
    assert resolver.last_resolved.secret_bytes.is_zeroized()

    resolver = _resolver(secret_validation_db)
    error_tester = SecretCredentialTester(
        resolver=resolver,
        settings=settings,
        client_factory=lambda **kwargs: _FakeClient(response=_FakeResponse(503, {"detail": "secret-canary-body"})),
    )
    with pytest.raises(SecretCredentialServiceUnavailableError):
        await error_tester.validate(context, "llm", "anthropic", "api_key")
    assert resolver.last_resolved.secret_bytes.is_zeroized()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_factory", "expected_error"),
    [
        (
            lambda: _FakeClientFactoryError(httpx.ConnectError("boom https://api.example host=secret-host")),
            "service_unavailable",
        ),
        (
            lambda: _FakeClientFactoryError(httpx.NetworkError("dns failed secret-host")),
            "service_unavailable",
        ),
        (
            lambda: _FakeClient(error=httpx.ConnectError("connect secret-host")),
            "service_unavailable",
        ),
        (
            lambda: _FakeClient(error=httpx.RequestError("tls secret-host", request=httpx.Request("GET", "https://api.example/models"))),
            "service_unavailable",
        ),
    ],
)
async def test_secret_credential_tester_maps_request_transport_errors_to_unavailable(
    secret_validation_db: Database,
    client_factory,
    expected_error,
):
    from multiclaw.secrets.validation import SecretCredentialServiceUnavailableError, SecretCredentialTester

    context = await _seed_context(secret_validation_db)
    await _seed_secret(secret_validation_db, context, provider_name="openai", plaintext="transport-secret")
    resolver = _resolver(secret_validation_db)
    settings = Settings(
        _config_file="/nonexistent",
        llm={"providers": {"openai": {"base_url": "https://api.openai.example/v1"}}},
    )
    tester = SecretCredentialTester(
        resolver=resolver,
        settings=settings,
        client_factory=lambda **kwargs: client_factory(),
    )

    assert expected_error == "service_unavailable"
    with pytest.raises(SecretCredentialServiceUnavailableError):
        await tester.validate(context, "llm", "openai", "api_key")

    assert resolver.last_resolved.secret_bytes.is_zeroized()


@pytest.mark.asyncio
async def test_secret_credential_tester_rejects_unsupported_provider_or_missing_base_url(secret_validation_db: Database):
    from multiclaw.secrets.validation import UnsupportedSecretValidationTargetError, SecretCredentialTester, SecretCredentialServiceUnavailableError

    context = await _seed_context(secret_validation_db)
    await _seed_secret(secret_validation_db, context, provider_name="openai")
    resolver = _resolver(secret_validation_db)

    unsupported = SecretCredentialTester(
        resolver=resolver,
        settings=Settings(_config_file="/nonexistent", llm={"providers": {"openai": {"base_url": "https://api.openai.example/v1"}}}),
        client_factory=lambda **kwargs: _FakeClient(response=_FakeResponse(200)),
    )
    with pytest.raises(UnsupportedSecretValidationTargetError):
        await unsupported.validate(context, "mcp", "demo", "api_key")
    with pytest.raises(UnsupportedSecretValidationTargetError):
        await unsupported.validate(context, "llm", "openai", "token")

    missing_base_url = SecretCredentialTester(
        resolver=resolver,
        settings=Settings(_config_file="/nonexistent", llm={"providers": {"openai": {"base_url": ""}}}),
        client_factory=lambda **kwargs: _FakeClient(response=_FakeResponse(200)),
    )
    with pytest.raises(SecretCredentialServiceUnavailableError):
        await missing_base_url.validate(context, "llm", "openai", "api_key")
