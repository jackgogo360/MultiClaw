import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import insert, text

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.secrets.envelope import EnvelopeFields, SecretEnvelopeService
from multiclaw.secrets.keyring import DeploymentKeyring
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, approval_requests
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.models import ApprovalStatus


TEST_JWT_SIGNING_KEY = "tenant-api-jwt-key-material-1234567890"


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


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


@dataclass(frozen=True)
class SeededIdentity:
    cookie: dict[str, str]
    tenant_id: str
    workspace_id: str
    auth_epoch: int
    email: str


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
    monkeypatch.setenv("MULTICLAW_SECRETS_KEYRING_B64", _keyring_payload())
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


@pytest.fixture(autouse=True)
def _csrf_test_defaults(monkeypatch: pytest.MonkeyPatch):
    original_request = TestClient.request

    def request_with_csrf(self, method, url, *args, **kwargs):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Origin", "http://testserver")
            headers.setdefault("X-CSRF-Token", "test-csrf-token")
            kwargs["headers"] = headers
            self.cookies.set("csrf_token", "test-csrf-token")
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", request_with_csrf)


async def _seed_identity(
    database: Database,
    *,
    email: str,
    age_seconds: int = 0,
) -> SeededIdentity:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        workspace_id = user.default_workspace_id
        assert workspace_id is not None

    issued_at = int(datetime.now(timezone.utc).timestamp()) - age_seconds
    token = jwt.encode(
        {
            "sub": user.id,
            "email": email,
            "auth_epoch": user.auth_epoch,
            "aud": "multiclaw-api",
            "iat": issued_at,
            "exp": issued_at + int(timedelta(days=1).total_seconds()),
        },
        TEST_JWT_SIGNING_KEY,
        algorithm="HS256",
    )
    return SeededIdentity(
        cookie={"token": token},
        tenant_id=user.id,
        workspace_id=workspace_id,
        auth_epoch=user.auth_epoch,
        email=email,
    )


async def _seed_secret(
    database: Database,
    identity: SeededIdentity,
    *,
    provider_kind: str,
    provider_name: str,
    secret_name: str,
    plaintext: str,
) -> None:
    keyring = DeploymentKeyring.load(
        object(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    envelope = SecretEnvelopeService(keyring)
    context = TenantContext(identity.tenant_id, identity.workspace_id)
    secret_id = str(uuid4())
    async with TenantUnitOfWork(database, context) as uow:
        await uow.secrets.put_encrypted(
            secret_id=secret_id,
            provider_kind=provider_kind,
            provider_name=provider_name,
            secret_name=secret_name,
            record=envelope.encrypt(
                plaintext.encode("utf-8"),
                EnvelopeFields(
                    tenant_id=identity.tenant_id,
                    workspace_id=None,
                    secret_id=secret_id,
                    provider_kind=provider_kind,
                    provider_name=provider_name,
                    secret_name=secret_name,
                ),
            ),
        )


async def _seed_approval(
    database: Database,
    identity: SeededIdentity,
    *,
    session_id: str,
    approval_status: ApprovalStatus,
    version: int = 1,
    expires_in_ms: int = 120_000,
) -> tuple[str, str]:
    run_id = str(uuid4())
    approval_id = str(uuid4())
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=identity.tenant_id,
                workspace_id=identity.workspace_id,
                session_id=session_id,
                run_status="awaiting_user",
                runtime_instance_id="runtime-test",
                lease_owner="runtime-test",
                fencing_token=1,
                lease_expires_at=now_ms + 60_000,
                heartbeat_at=now_ms,
                schema_version=1,
                version=1,
                created_at=now_ms,
                updated_at=now_ms,
                finished_at=None,
            )
        )
        await conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=identity.tenant_id,
                workspace_id=identity.workspace_id,
                session_id=session_id,
                run_id=run_id,
                tool_call_id="tool-call-1",
                approval_status=approval_status.value,
                requested_at=now_ms,
                resolved_at=None if approval_status is ApprovalStatus.AWAITING_USER else now_ms,
                expires_at=now_ms + expires_in_ms,
                version=version,
            )
        )
    return approval_id, run_id


def test_foreign_and_missing_session_message_routes_are_indistinguishable_404(migrated_database: Database):
    import multiclaw.server as server

    owner = asyncio.run(_seed_identity(migrated_database, email="owner@example.com"))
    foreign = asyncio.run(_seed_identity(migrated_database, email="foreign@example.com"))

    with TestClient(server.app) as client:
        client.cookies = owner.cookie
        created = client.post("/api/sessions", json={"title": "Owner Session"}).json()

    with TestClient(server.app) as client:
        client.cookies = foreign.cookie
        foreign_response = client.get(f"/api/sessions/{created['id']}/messages")
        missing_response = client.get(f"/api/sessions/{uuid4()}/messages")

    assert foreign_response.status_code == 404
    assert missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json() == {"detail": "session not found"}


def test_chat_only_creates_session_when_no_session_id_is_supplied(migrated_database: Database):
    import multiclaw.server as server

    identity = asyncio.run(_seed_identity(migrated_database, email="chat@example.com"))

    with TestClient(server.app) as client:
        client.cookies = identity.cookie
        assert client.get("/api/sessions").json() == []

        missing = client.post(
            "/api/chat",
            json={"message": "hello", "session_id": str(uuid4())},
        )
        assert missing.status_code == 404
        assert client.get("/api/sessions").json() == []

        empty_alias = client.post(
            "/api/chat",
            json={"message": "hello", "id": ""},
        )
        assert empty_alias.status_code == 404
        assert client.get("/api/sessions").json() == []

        created = client.post("/api/chat", json={"message": "hello"})
        listed = client.get("/api/sessions").json()

    assert created.status_code == 200
    assert len(listed) == 1


def test_approval_endpoint_matrix_enforces_scope_expiry_and_cas(migrated_database: Database):
    import multiclaw.server as server

    owner = asyncio.run(_seed_identity(migrated_database, email="approvals-owner@example.com"))
    foreign = asyncio.run(_seed_identity(migrated_database, email="approvals-foreign@example.com"))

    with TestClient(server.app) as client:
        client.cookies = owner.cookie
        session = client.post("/api/sessions", json={"title": "Approval Session"}).json()

    awaiting_id, _ = asyncio.run(
        _seed_approval(
            migrated_database,
            owner,
            session_id=session["id"],
            approval_status=ApprovalStatus.AWAITING_USER,
        )
    )
    expired_id, _ = asyncio.run(
        _seed_approval(
            migrated_database,
            owner,
            session_id=session["id"],
            approval_status=ApprovalStatus.AWAITING_USER,
            expires_in_ms=-1,
        )
    )
    resolved_id, _ = asyncio.run(
        _seed_approval(
            migrated_database,
            owner,
            session_id=session["id"],
            approval_status=ApprovalStatus.REJECTED,
            version=2,
        )
    )

    with TestClient(server.app) as client:
        client.cookies = owner.cookie
        missing_get = client.get(f"/api/approvals/{uuid4()}")
        missing_decision = client.post(
            f"/api/approvals/{uuid4()}/decision",
            json={"approved": True, "version": 1},
        )
        approved = client.post(
            f"/api/approvals/{awaiting_id}/decision",
            json={"approved": True, "version": 1},
        )
        version_conflict = client.post(
            f"/api/approvals/{awaiting_id}/decision",
            json={"approved": False, "version": 1},
        )
        expired = client.post(
            f"/api/approvals/{expired_id}/decision",
            json={"approved": True, "version": 1},
        )
        resolved = client.post(
            f"/api/approvals/{resolved_id}/decision",
            json={"approved": True, "version": 2},
        )

    with TestClient(server.app) as client:
        client.cookies = foreign.cookie
        foreign_get = client.get(f"/api/approvals/{awaiting_id}")
        foreign_decision = client.post(
            f"/api/approvals/{awaiting_id}/decision",
            json={"approved": False, "version": 2},
        )

    assert missing_get.status_code == 404
    assert missing_decision.status_code == 404
    assert foreign_get.status_code == 404
    assert foreign_decision.status_code == 404
    assert approved.status_code == 200
    assert version_conflict.status_code == 409
    assert expired.status_code == 410
    assert resolved.status_code == 409


def test_secret_api_only_returns_metadata_and_requires_recent_reauth(migrated_database: Database):
    import multiclaw.server as server

    fresh = asyncio.run(_seed_identity(migrated_database, email="fresh@example.com"))
    stale = asyncio.run(
        _seed_identity(
            migrated_database,
            email="stale@example.com",
            age_seconds=16 * 60,
        )
    )
    asyncio.run(
        _seed_secret(
            migrated_database,
            fresh,
            provider_kind="llm",
            provider_name="openai",
            secret_name="api_key",
            plaintext="plain-secret-value",
        )
    )

    with TestClient(server.app) as client:
        client.cookies = fresh.cookie
        listed = client.get("/api/secrets")
        tested = client.post("/api/secrets/llm:openai/api_key/test")

    with TestClient(server.app) as client:
        client.cookies = stale.cookie
        stale_put = client.put(
            "/api/secrets/llm:openai/api_key",
            json={"value": "new-plain-secret"},
        )
        stale_delete = client.delete("/api/secrets/llm:openai/api_key")
        stale_test = client.post("/api/secrets/llm:openai/api_key/test")

    assert listed.status_code == 200
    listed_payload = listed.json()
    listed_text = json.dumps(listed_payload)
    assert "plain-secret-value" not in listed_text
    assert "plain-secret-value" not in tested.text
    assert listed_payload and listed_payload[0]["masked_value"].startswith("****")
    assert "value" not in listed_payload[0]
    assert tested.status_code == 200
    assert stale_put.status_code == 401
    assert stale_delete.status_code == 401
    assert stale_test.status_code == 401


def test_secret_api_is_tenant_scoped(migrated_database: Database):
    import multiclaw.server as server

    owner = asyncio.run(_seed_identity(migrated_database, email="secret-owner@example.com"))
    foreign = asyncio.run(_seed_identity(migrated_database, email="secret-foreign@example.com"))
    asyncio.run(
        _seed_secret(
            migrated_database,
            owner,
            provider_kind="llm",
            provider_name="openai",
            secret_name="api_key",
            plaintext="tenant-only-secret",
        )
    )

    with TestClient(server.app) as client:
        client.cookies = owner.cookie
        owner_list = client.get("/api/secrets")

    with TestClient(server.app) as client:
        client.cookies = foreign.cookie
        foreign_list = client.get("/api/secrets")
        foreign_delete = client.delete("/api/secrets/llm:openai/api_key")

    assert owner_list.status_code == 200
    assert foreign_list.status_code == 200
    assert owner_list.json() != []
    assert foreign_list.json() == []
    assert foreign_delete.status_code == 404


def test_chat_returns_429_when_tenant_quota_is_exhausted(migrated_database: Database, monkeypatch: pytest.MonkeyPatch):
    import multiclaw.server as server
    from multiclaw.workflow.models import TenantRunQuotaError

    class _Runtime:
        runtime_instance_id = "runtime-tenant-quota"
        agent = type("Agent", (), {"handle_message_stream": None})()
        event_router = type("EventRouter", (), {"subscribe": lambda *args, **kwargs: None})()

        def begin_run(self):
            return object()

    class _Coordinator:
        async def start_run_with_checkpoint(self, *args, **kwargs):
            raise TenantRunQuotaError("tenant run quota exceeded")

        async def finish_run_with_checkpoint(self, *args, **kwargs):
            return None

        async def finish_run(self, *args, **kwargs):
            return None

    async def fake_acquire(context):
        del context
        return _Runtime()

    identity = asyncio.run(_seed_identity(migrated_database, email="quota@example.com"))

    with TestClient(server.app) as client:
        client.cookies = identity.cookie
        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", fake_acquire)
        monkeypatch.setattr(server, "build_workflow_coordinator", lambda *args, **kwargs: _Coordinator())
        response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 429
    assert response.json() == {"detail": "tenant run quota exceeded"}
