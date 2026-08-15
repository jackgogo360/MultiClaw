import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


async def _create_database(tmp_path: Path) -> Database:
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=database_url))


@dataclass(frozen=True)
class SeededIdentity:
    cookie: dict[str, str]
    tenant_id: str
    workspace_id: str | None
    auth_epoch: int
    email: str


@dataclass(frozen=True)
class TwoUsers:
    a: SeededIdentity
    b: SeededIdentity
    session_id: str


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


@pytest.fixture
def client(migrated_database: Database):
    del migrated_database
    import multiclaw.server as server

    with TestClient(server.app) as test_client:
        yield test_client


def _make_cookie(
    secret: str,
    *,
    user_id: str,
    email: str,
    auth_epoch: int | None,
) -> dict[str, str]:
    claims = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=1),
    }
    if auth_epoch is not None:
        claims["auth_epoch"] = auth_epoch
    return {"token": jwt.encode(claims, secret, algorithm="HS256")}


def _force_default_workspace_id(database: Database, user_id: str, workspace_id: str) -> None:
    database_path = database.engine.url.database
    assert database_path is not None
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            UPDATE users
            SET default_workspace_id = ?,
                updated_at = updated_at + 1
            WHERE id = ?
            """,
            (workspace_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _seed_identity(
    database: Database,
    secret: str,
    *,
    email: str,
    status: str = "active",
    auth_epoch: int = 0,
    workspace_mode: str = "default",
    token_auth_epoch: int | None | object = ...,
) -> SeededIdentity:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        workspace_id = user.default_workspace_id
        assert workspace_id is not None

        if auth_epoch != user.auth_epoch or status != "active":
            await uow.conn.execute(
                text(
                    """
                    UPDATE users
                    SET auth_epoch = :auth_epoch,
                        status = :status,
                        updated_at = updated_at + 1
                    WHERE id = :user_id
                    """
                ),
                {"auth_epoch": auth_epoch, "status": status, "user_id": user.id},
            )

        if workspace_mode == "null":
            workspace_id = None
            await uow.conn.execute(
                text(
                    """
                    UPDATE users
                    SET default_workspace_id = NULL,
                        updated_at = updated_at + 1
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user.id},
            )
        elif workspace_mode == "missing":
            workspace_id = f"workspace-missing-{uuid4()}"
        elif workspace_mode.startswith("foreign:"):
            workspace_id = workspace_mode.split(":", 1)[1]

    if workspace_mode == "missing" or workspace_mode.startswith("foreign:"):
        await asyncio.to_thread(_force_default_workspace_id, database, user.id, workspace_id)

    effective_auth_epoch = auth_epoch if token_auth_epoch is ... else token_auth_epoch
    cookie = _make_cookie(
        secret,
        user_id=user.id,
        email=email,
        auth_epoch=effective_auth_epoch,
    )
    return SeededIdentity(
        cookie=cookie,
        tenant_id=user.id,
        workspace_id=workspace_id,
        auth_epoch=auth_epoch,
        email=email,
    )


async def _get_user_row(database: Database, email: str) -> dict[str, object]:
    async with database.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT id, email, auth_epoch, default_workspace_id, status
                    FROM users
                    WHERE email = :email
                    """
                ),
                {"email": email},
            )
        ).mappings().one()
    return dict(row)


@pytest.fixture
def user_a_cookie(client: TestClient, migrated_database: Database) -> SeededIdentity:
    secret = client.app.state.auth_store.jwt_secret
    return asyncio.run(_seed_identity(migrated_database, secret, email="user-a@example.com"))


@pytest.fixture
def two_users(client: TestClient, migrated_database: Database) -> TwoUsers:
    secret = client.app.state.auth_store.jwt_secret
    user_a = asyncio.run(_seed_identity(migrated_database, secret, email="user-a@example.com"))
    user_b = asyncio.run(_seed_identity(migrated_database, secret, email="user-b@example.com"))
    created = client.post("/api/sessions", cookies=user_b.cookie, json={"title": "Owned by B"})
    assert created.status_code == 200
    return TwoUsers(a=user_a, b=user_b, session_id=created.json()["id"])


@pytest.fixture
def owned_session(client: TestClient, user_a_cookie: SeededIdentity) -> str:
    created = client.post(
        "/api/sessions",
        cookies=user_a_cookie.cookie,
        json={"title": "Owned by A"},
    )
    assert created.status_code == 200
    return created.json()["id"]


def test_request_tenant_ignores_spoofed_headers_and_body(client: TestClient, user_a_cookie: SeededIdentity):
    response = client.post(
        "/api/sessions",
        cookies=user_a_cookie.cookie,
        headers={"X-Tenant-Id": "tenant-b", "X-Workspace-Id": "workspace-b"},
        json={"title": "Owned", "tenant_id": "tenant-b", "workspace_id": "workspace-b"},
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == user_a_cookie.tenant_id
    assert response.json()["workspace_id"] == user_a_cookie.workspace_id


def test_real_login_cookie_uses_current_db_auth_epoch_and_authenticates_protected_routes(
    client: TestClient,
    migrated_database: Database,
):
    email = "verify-flow@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True

    send_response = client.post(
        "/auth/send-code",
        json={"email": email},
    )
    verify_response = client.post(
        "/auth/verify",
        json={"email": email, "code": "654321"},
    )
    me_response = client.get("/auth/me")
    sessions_response = client.get("/api/sessions")

    assert send_response.status_code == 200
    assert verify_response.status_code == 200
    assert me_response.status_code == 200
    assert sessions_response.status_code == 200

    persisted_user = asyncio.run(_get_user_row(migrated_database, email))
    token = client.cookies.get("token")
    assert token is not None
    payload = jwt.decode(token, client.app.state.auth_store.jwt_secret, algorithms=["HS256"])

    assert payload["sub"] == persisted_user["id"]
    assert payload["auth_epoch"] == persisted_user["auth_epoch"]
    assert "iat" in payload
    assert "exp" in payload
    assert me_response.json() == {"email": email, "user_id": persisted_user["id"]}


def test_foreign_session_id_is_404_and_does_not_create_session(client: TestClient, two_users: TwoUsers):
    before = client.get("/api/sessions", cookies=two_users.a.cookie).json()
    response = client.post(
        "/api/chat",
        cookies=two_users.a.cookie,
        json={"message": "hello", "session_id": two_users.session_id},
    )
    after = client.get("/api/sessions", cookies=two_users.a.cookie).json()

    assert response.status_code == 404
    assert after == before


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        ({"message": "hello", "session_id": ""}, "empty session_id"),
        ({"message": "hello", "session_id": "session-missing"}, "missing session_id"),
        ({"message": "hello", "id": ""}, "empty id"),
        ({"message": "hello", "id": "session-missing"}, "missing id"),
    ],
)
def test_invalid_explicit_session_identifier_is_404_and_does_not_create_session(
    client: TestClient,
    user_a_cookie: SeededIdentity,
    payload: dict[str, str],
    label: str,
):
    before = client.get("/api/sessions", cookies=user_a_cookie.cookie).json()
    response = client.post("/api/chat", cookies=user_a_cookie.cookie, json=payload)
    after = client.get("/api/sessions", cookies=user_a_cookie.cookie).json()

    assert response.status_code == 404, label
    assert after == before, label


def test_explicit_empty_session_id_takes_precedence_over_valid_id_and_does_not_create_session(
    client: TestClient,
    user_a_cookie: SeededIdentity,
    owned_session: str,
):
    before = client.get("/api/sessions", cookies=user_a_cookie.cookie).json()
    response = client.post(
        "/api/chat",
        cookies=user_a_cookie.cookie,
        json={"message": "hello", "session_id": "", "id": owned_session},
    )
    after = client.get("/api/sessions", cookies=user_a_cookie.cookie).json()

    assert response.status_code == 404
    assert after == before


@pytest.mark.parametrize("status", ["disabled", "pending_purge"])
def test_unavailable_user_status_returns_403(
    client: TestClient,
    migrated_database: Database,
    status: str,
):
    secret = client.app.state.auth_store.jwt_secret
    identity = asyncio.run(
        _seed_identity(migrated_database, secret, email=f"{status}@example.com", status=status)
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account unavailable"}


@pytest.mark.parametrize("workspace_mode", ["null", "missing"])
def test_invalid_default_workspace_returns_403(
    client: TestClient,
    migrated_database: Database,
    workspace_mode: str,
):
    secret = client.app.state.auth_store.jwt_secret
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            secret,
            email=f"{workspace_mode}@example.com",
            workspace_mode=workspace_mode,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account unavailable"}


def test_foreign_default_workspace_returns_403(client: TestClient, migrated_database: Database):
    secret = client.app.state.auth_store.jwt_secret
    owner = asyncio.run(_seed_identity(migrated_database, secret, email="owner@example.com"))
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            secret,
            email="foreign-workspace@example.com",
            workspace_mode=f"foreign:{owner.workspace_id}",
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account unavailable"}


def test_no_token_requests_stay_401(client: TestClient):
    response = client.get("/api/sessions")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "tenant-id", "auth_epoch": True},
        {"sub": "tenant-id", "auth_epoch": "0"},
        {"sub": "tenant-id", "auth_epoch": 0.5},
        {"sub": 123, "auth_epoch": 0},
        {"sub": "", "auth_epoch": 0},
    ],
)
def test_malformed_signed_claims_are_401(
    client: TestClient,
    user_a_cookie: SeededIdentity,
    claims: dict[str, object],
):
    token = jwt.encode(
        {
            **claims,
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        client.app.state.auth_store.jwt_secret,
        algorithm="HS256",
    )

    response = client.get("/api/sessions", cookies={"token": token})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_auth_me_does_not_acquire_write_lock_for_authenticated_lookup(
    client: TestClient,
    user_a_cookie: SeededIdentity,
    monkeypatch: pytest.MonkeyPatch,
):
    async def _unexpected_begin_write(_connection):
        raise AssertionError("begin_write should not be called")

    monkeypatch.setattr(client.app.state.database.dialect, "begin_write", _unexpected_begin_write)

    response = client.get("/auth/me", cookies=user_a_cookie.cookie)

    assert response.status_code == 200
    assert response.json() == {"email": user_a_cookie.email, "user_id": user_a_cookie.tenant_id}


def test_missing_auth_epoch_is_401_before_scope_resolution(client: TestClient, migrated_database: Database):
    secret = client.app.state.auth_store.jwt_secret
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            secret,
            email="missing-epoch@example.com",
            workspace_mode="null",
            token_auth_epoch=None,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_auth_epoch_mismatch_is_401_before_scope_resolution(client: TestClient, migrated_database: Database):
    secret = client.app.state.auth_store.jwt_secret
    owner = asyncio.run(_seed_identity(migrated_database, secret, email="epoch-owner@example.com"))
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            secret,
            email="epoch-mismatch@example.com",
            auth_epoch=7,
            workspace_mode=f"foreign:{owner.workspace_id}",
            token_auth_epoch=6,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
