import asyncio
import hmac
import sqlite3
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
from multiclaw.auth.models import MAX_SENDS_PER_DAY, issue_verification_code
from multiclaw.auth.cleanup import AuthCleanupWorker
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import verification_codes
from multiclaw.storage.uow import AuthUnitOfWork


TEST_JWT_SIGNING_KEY = "test-jwt-signing-key-material-1234567890"


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
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
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


def _make_cookie(
    signing_key: str,
    *,
    user_id: str,
    email: str,
    auth_epoch: int | None,
    audience: str = "multiclaw-api",
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> dict[str, str]:
    now = issued_at or int(datetime.now(timezone.utc).timestamp())
    claims = {
        "sub": user_id,
        "email": email,
        "aud": audience,
        "iat": now,
        "exp": expires_at if expires_at is not None else now + int(timedelta(days=1).total_seconds()),
    }
    if auth_epoch is not None:
        claims["auth_epoch"] = auth_epoch
    return {"token": jwt.encode(claims, signing_key, algorithm="HS256")}


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


async def _get_verification_rows(database: Database, email: str) -> list[dict[str, object]]:
    async with database.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT id, email, code_digest, purpose, expires_at, used_at, created_at
                    FROM verification_codes
                    WHERE email = :email
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"email": email},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def _seed_verification_row(
    database: Database,
    *,
    email: str,
    code_digest: str,
    purpose: str,
    expires_at: int,
    created_at: int,
) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email=email,
                code_digest=code_digest,
                purpose=purpose,
                expires_at=expires_at,
                used_at=None,
                created_at=created_at,
            )
        )


async def _db_now_seconds(database: Database) -> int:
    async with AuthUnitOfWork(database, read_only=True) as uow:
        return await uow.verification_codes.db_now_ms() // 1000


@pytest.fixture
def user_a_cookie(client: TestClient, migrated_database: Database) -> SeededIdentity:
    return asyncio.run(
        _seed_identity(migrated_database, TEST_JWT_SIGNING_KEY, email="user-a@example.com")
    )


@pytest.fixture
def two_users(client: TestClient, migrated_database: Database) -> TwoUsers:
    user_a = asyncio.run(
        _seed_identity(migrated_database, TEST_JWT_SIGNING_KEY, email="user-a@example.com")
    )
    user_b = asyncio.run(
        _seed_identity(migrated_database, TEST_JWT_SIGNING_KEY, email="user-b@example.com")
    )
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
    client.app.state.auth_forced_code = "654321"

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
    payload = jwt.decode(
        token,
        TEST_JWT_SIGNING_KEY,
        algorithms=["HS256"],
        audience="multiclaw-api",
    )

    assert payload["sub"] == persisted_user["id"]
    assert payload["email"] == email
    assert payload["aud"] == "multiclaw-api"
    assert payload["auth_epoch"] == persisted_user["auth_epoch"]
    assert "iat" in payload
    assert "exp" in payload
    assert me_response.json() == {"email": email, "user_id": persisted_user["id"]}


def test_send_code_persists_domain_separated_digest_without_plaintext_leak(
    client: TestClient,
    migrated_database: Database,
    caplog: pytest.LogCaptureFixture,
):
    email = "digest-check@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "654321"

    caplog.set_level("INFO", logger="multiclaw")
    response = client.post("/auth/send-code", json={"email": email})
    rows = asyncio.run(_get_verification_rows(migrated_database, email))

    digest_key = hmac.digest(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        b"multiclaw.verification-code-key.v1",
        "sha256",
    )
    expected_digest = hmac.new(
        digest_key,
        b"\0".join((b"login", email.encode("utf-8"), b"654321")),
        "sha256",
    ).hexdigest()

    assert response.status_code == 200
    assert rows == [
        {
            "id": rows[0]["id"],
            "email": email,
            "code_digest": expected_digest,
            "purpose": "login",
            "expires_at": rows[0]["expires_at"],
            "used_at": None,
            "created_at": rows[0]["created_at"],
        }
    ]
    assert rows[0]["code_digest"] != "654321"
    assert "654321" not in caplog.text
    assert "654321" not in response.text


def test_send_code_rate_limits_per_normalized_email_and_login_purpose_only(
    client: TestClient,
    migrated_database: Database,
):
    email = "rate-limit@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True

    now_ms = asyncio.run(_db_now_seconds(migrated_database)) * 1000
    asyncio.run(
        _seed_verification_row(
            migrated_database,
            email=email,
            code_digest="deletion-recovery-digest",
            purpose="deletion_recovery",
            expires_at=now_ms + 60_000,
            created_at=now_ms - 1_000,
        )
    )

    responses = [
        client.post("/auth/send-code", json={"email": f"  {email.upper()}  "})
        for _ in range(MAX_SENDS_PER_DAY)
    ]
    limited = client.post("/auth/send-code", json={"email": email})
    rows = asyncio.run(_get_verification_rows(migrated_database, email))

    assert [response.status_code for response in responses] == [200] * MAX_SENDS_PER_DAY
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts, please try again tomorrow"}
    assert [row["purpose"] for row in rows].count("login") == MAX_SENDS_PER_DAY
    assert [row["purpose"] for row in rows].count("deletion_recovery") == 1


def test_send_code_failure_does_not_persist_code_or_consume_quota(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import multiclaw.auth.router as auth_router

    email = "send-failure@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    caplog.set_level("ERROR", logger="multiclaw")

    async def fail_sender(*_args, **_kwargs):
        raise RuntimeError("smtp failure secret-token")

    async def succeed_sender(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_router, "send_verification_code", fail_sender)
    failed = client.post("/auth/send-code", json={"email": email})
    rows_after_failure = asyncio.run(_get_verification_rows(migrated_database, email))

    monkeypatch.setattr(auth_router, "send_verification_code", succeed_sender)
    success_responses = [
        client.post("/auth/send-code", json={"email": email})
        for _ in range(MAX_SENDS_PER_DAY)
    ]
    limited = client.post("/auth/send-code", json={"email": email})
    rows_after_success = asyncio.run(_get_verification_rows(migrated_database, email))

    assert failed.status_code == 502
    assert rows_after_failure == []
    assert "secret-token" not in caplog.text
    assert [response.status_code for response in success_responses] == [200] * MAX_SENDS_PER_DAY
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts, please try again tomorrow"}
    assert [row["purpose"] for row in rows_after_success].count("login") == MAX_SENDS_PER_DAY


def test_deletion_recovery_audience_is_rejected_by_normal_api(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-audience@example.com",
        )
    )
    recovery_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=identity.tenant_id,
        email=identity.email,
        auth_epoch=identity.auth_epoch,
        audience="multiclaw-deletion-recovery",
    )

    response = client.get("/api/sessions", cookies=recovery_cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_deletion_recovery_jwt_claim_shape():
    from multiclaw.auth.router import _make_deletion_recovery_jwt

    token = _make_deletion_recovery_jwt(
        user_id="user-123",
        email="user@example.com",
        job_id="job-456",
        signing_key=TEST_JWT_SIGNING_KEY.encode("utf-8"),
        issued_at=1_700_000_000,
    )
    payload = jwt.decode(
        token,
        TEST_JWT_SIGNING_KEY,
        algorithms=["HS256"],
        audience="multiclaw-deletion-recovery",
        options={"verify_exp": False},
    )

    assert payload["sub"] == "user-123"
    assert payload["email"] == "user@example.com"
    assert payload["aud"] == "multiclaw-deletion-recovery"
    assert payload["purpose"] == "deletion_recovery"
    assert payload["job_id"] == "job-456"
    assert payload["exp"] - payload["iat"] == 600


@pytest.mark.asyncio
async def test_verification_codes_ignore_wrong_purpose_and_are_consumed_once_atomically(
    migrated_database: Database,
) -> None:
    async with AuthUnitOfWork(migrated_database) as uow:
        await uow.conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email="atomic@example.com",
                code_digest="digest-a",
                purpose="deletion_recovery",
                expires_at=uow._database.dialect.db_now_ms() + 60_000,
                used_at=None,
                created_at=uow._database.dialect.db_now_ms(),
            )
        )
        await uow.conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email="atomic@example.com",
                code_digest="digest-b",
                purpose="login",
                expires_at=uow._database.dialect.db_now_ms() + 60_000,
                used_at=None,
                created_at=uow._database.dialect.db_now_ms() + 1,
            )
        )

    async with AuthUnitOfWork(migrated_database) as uow:
        assert await uow.verification_codes.consume_latest_code(
            email="atomic@example.com",
            purpose="login",
            code_digest="digest-a",
        ) is None
        first = await uow.verification_codes.consume_latest_code(
            email="atomic@example.com",
            purpose="login",
            code_digest="digest-b",
        )
        second = await uow.verification_codes.consume_latest_code(
            email="atomic@example.com",
            purpose="login",
            code_digest="digest-b",
        )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_auth_cleanup_worker_deletes_only_codes_expired_by_db_clock(
    migrated_database: Database,
) -> None:
    now_ms = await _db_now_seconds(migrated_database) * 1000
    await _seed_verification_row(
        migrated_database,
        email="expired-login@example.com",
        code_digest="expired-login",
        purpose="login",
        expires_at=now_ms - 1,
        created_at=now_ms - 10_000,
    )
    await _seed_verification_row(
        migrated_database,
        email="equal-recovery@example.com",
        code_digest="equal-recovery",
        purpose="deletion_recovery",
        expires_at=now_ms,
        created_at=now_ms - 9_000,
    )
    await _seed_verification_row(
        migrated_database,
        email="live-login@example.com",
        code_digest="live-login",
        purpose="login",
        expires_at=now_ms + 60_000,
        created_at=now_ms - 8_000,
    )
    await _seed_verification_row(
        migrated_database,
        email="live-recovery@example.com",
        code_digest="live-recovery",
        purpose="deletion_recovery",
        expires_at=now_ms + 120_000,
        created_at=now_ms - 7_000,
    )

    deleted = await AuthCleanupWorker(migrated_database).run_once()

    assert deleted == 2
    assert await _get_verification_rows(migrated_database, "expired-login@example.com") == []
    assert await _get_verification_rows(migrated_database, "equal-recovery@example.com") == []
    assert len(await _get_verification_rows(migrated_database, "live-login@example.com")) == 1
    assert len(await _get_verification_rows(migrated_database, "live-recovery@example.com")) == 1


def test_only_latest_unused_login_code_is_valid_in_frozen_layout(
    client: TestClient,
    migrated_database: Database,
):
    email = "stale-code@example.com"
    old_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email=email,
        forced_code="111111",
    ).code_digest
    new_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email=email,
        forced_code="222222",
    ).code_digest
    asyncio.run(
        _seed_verification_row(
            migrated_database,
            email=email,
            code_digest=old_digest,
            purpose="login",
            expires_at=1_900_000_000_000,
            created_at=1_800_000_000_000,
        )
    )
    asyncio.run(
        _seed_verification_row(
            migrated_database,
            email=email,
            code_digest=new_digest,
            purpose="login",
            expires_at=1_900_000_000_001,
            created_at=1_800_000_000_001,
        )
    )

    old_response = client.post("/auth/verify", json={"email": email, "code": "111111"})
    rows_after_old = asyncio.run(_get_verification_rows(migrated_database, email))
    me_after_old = client.get("/auth/me")
    new_response = client.post("/auth/verify", json={"email": email, "code": "222222"})
    rows_after_new = asyncio.run(_get_verification_rows(migrated_database, email))

    assert old_response.status_code == 401
    assert me_after_old.json() == {"email": None, "user_id": None}
    assert [row["used_at"] for row in rows_after_old] == [None, None]
    assert new_response.status_code == 200
    assert rows_after_new[0]["used_at"] is None
    assert rows_after_new[1]["used_at"] is not None


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
def test_unavailable_user_status_returns_401(
    client: TestClient,
    migrated_database: Database,
    status: str,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email=f"{status}@example.com",
            status=status,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize("workspace_mode", ["null", "missing"])
def test_invalid_default_workspace_returns_403(
    client: TestClient,
    migrated_database: Database,
    workspace_mode: str,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email=f"{workspace_mode}@example.com",
            workspace_mode=workspace_mode,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account unavailable"}


def test_foreign_default_workspace_returns_403(client: TestClient, migrated_database: Database):
    owner = asyncio.run(
        _seed_identity(migrated_database, TEST_JWT_SIGNING_KEY, email="owner@example.com")
    )
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="foreign-workspace@example.com",
            workspace_mode=f"foreign:{owner.workspace_id}",
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account unavailable"}


def test_delete_session_requires_recent_auth_using_db_clock_boundary(
    client: TestClient,
    migrated_database: Database,
    user_a_cookie: SeededIdentity,
):
    created_boundary = client.post("/api/sessions", cookies=user_a_cookie.cookie, json={"title": "Boundary"})
    created_stale = client.post("/api/sessions", cookies=user_a_cookie.cookie, json={"title": "Stale"})
    assert created_boundary.status_code == 200
    assert created_stale.status_code == 200
    boundary_session_id = created_boundary.json()["id"]
    stale_session_id = created_stale.json()["id"]
    now_seconds = asyncio.run(_db_now_seconds(migrated_database))

    boundary_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=user_a_cookie.tenant_id,
        email=user_a_cookie.email,
        auth_epoch=user_a_cookie.auth_epoch,
        issued_at=now_seconds - 300,
        expires_at=now_seconds + 3600,
    )
    stale_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=user_a_cookie.tenant_id,
        email=user_a_cookie.email,
        auth_epoch=user_a_cookie.auth_epoch,
        issued_at=now_seconds - 301,
        expires_at=now_seconds + 3600,
    )

    boundary_response = client.delete(f"/api/sessions/{boundary_session_id}", cookies=boundary_cookie)
    stale_response = client.delete(f"/api/sessions/{stale_session_id}", cookies=stale_cookie)

    assert boundary_response.status_code == 200
    assert stale_response.status_code == 401
    assert stale_response.json() == {"detail": "Recent authentication required"}


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
            "aud": "multiclaw-api",
            "iat": int(datetime.now(timezone.utc).timestamp()),
        },
        TEST_JWT_SIGNING_KEY,
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
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="missing-epoch@example.com",
            workspace_mode="null",
            token_auth_epoch=None,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_auth_epoch_mismatch_is_401_before_scope_resolution(client: TestClient, migrated_database: Database):
    owner = asyncio.run(
        _seed_identity(migrated_database, TEST_JWT_SIGNING_KEY, email="epoch-owner@example.com")
    )
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="epoch-mismatch@example.com",
            auth_epoch=7,
            workspace_mode=f"foreign:{owner.workspace_id}",
            token_auth_epoch=6,
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
