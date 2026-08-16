import asyncio
import hmac
import sqlite3
import threading
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


def _latest_cookie_value(client: TestClient, name: str) -> str | None:
    value: str | None = None
    for cookie in client.cookies.jar:
        if cookie.name == name:
            value = cookie.value
    return value


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


async def _get_deletion_state(database: Database, tenant_id: str) -> dict[str, object] | None:
    async with database.connect() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    SELECT
                        users.status AS user_status,
                        users.auth_epoch AS auth_epoch,
                        users.purge_after AS user_purge_after,
                        deletion_jobs.job_id AS job_id,
                        deletion_jobs.status AS job_status,
                        deletion_jobs.requested_at AS requested_at,
                        deletion_jobs.purge_after AS job_purge_after
                    FROM users
                    LEFT JOIN deletion_jobs ON deletion_jobs.tenant_id = users.id
                    WHERE users.id = :tenant_id
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).mappings().first()
    return None if row is None else dict(row)


async def _seed_active_run(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
    lease_expires_at: int,
) -> None:
    session_id = str(uuid4())
    run_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    id,
                    tenant_id,
                    workspace_id,
                    title,
                    status,
                    created_at,
                    updated_at,
                    last_message_at,
                    metadata_json
                ) VALUES (
                    :session_id,
                    :tenant_id,
                    :workspace_id,
                    'Blocking run',
                    'active',
                    1,
                    1,
                    NULL,
                    '{}'
                )
                """
            ),
            {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO agent_runs (
                    run_id,
                    tenant_id,
                    workspace_id,
                    session_id,
                    run_status,
                    runtime_instance_id,
                    lease_owner,
                    fencing_token,
                    lease_expires_at,
                    heartbeat_at,
                    schema_version,
                    version,
                    created_at,
                    updated_at,
                    finished_at
                ) VALUES (
                    :run_id,
                    :tenant_id,
                    :workspace_id,
                    :session_id,
                    'running',
                    'runtime-blocking',
                    'runtime-blocking',
                    1,
                    :lease_expires_at,
                    1,
                    1,
                    1,
                    1,
                    1,
                    NULL
                )
                """
            ),
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "lease_expires_at": lease_expires_at,
            },
        )


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
    now_ms = asyncio.run(_db_now_seconds(migrated_database)) * 1000
    asyncio.run(
        _seed_verification_row(
            migrated_database,
            email=email,
            code_digest="existing-login-digest",
            purpose="login",
            expires_at=now_ms + 60_000,
            created_at=now_ms - 5_000,
        )
    )

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
        for _ in range(MAX_SENDS_PER_DAY - 1)
    ]
    limited = client.post("/auth/send-code", json={"email": email})
    rows_after_success = asyncio.run(_get_verification_rows(migrated_database, email))

    assert failed.status_code == 502
    assert len(rows_after_failure) == 1
    assert rows_after_failure[0]["code_digest"] == "existing-login-digest"
    assert "secret-token" not in caplog.text
    assert [response.status_code for response in success_responses] == [200] * (MAX_SENDS_PER_DAY - 1)
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts, please try again tomorrow"}
    assert [row["purpose"] for row in rows_after_success].count("login") == MAX_SENDS_PER_DAY


def test_send_code_provider_io_does_not_hold_sqlite_write_transaction(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.auth.router as auth_router

    email = "slow-provider@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    provider_started = threading.Event()
    release_provider = threading.Event()
    response_holder: dict[str, object] = {}
    db_url = migrated_database.engine.url.render_as_string(hide_password=False)
    fast_writer_db = Database.create(
        DatabaseSettings(driver="sqlite", url=db_url, sqlite_busy_timeout_ms=100)
    )

    async def slow_sender(*_args, **_kwargs):
        provider_started.set()
        released = await asyncio.to_thread(release_provider.wait, 5)
        if not released:
            raise RuntimeError("provider wait timed out")

    def do_request() -> None:
        response_holder["response"] = client.post("/auth/send-code", json={"email": email})

    async def unrelated_write() -> None:
        async with fast_writer_db.write_transaction() as conn:
            await conn.execute(
                insert(verification_codes).values(
                    id=str(uuid4()),
                    email="unrelated@example.com",
                    code_digest="unrelated-digest",
                    purpose="login",
                    expires_at=fast_writer_db.dialect.db_now_ms() + 60_000,
                    used_at=None,
                    created_at=fast_writer_db.dialect.db_now_ms(),
                )
            )

    monkeypatch.setattr(auth_router, "send_verification_code", slow_sender)
    request_thread = threading.Thread(target=do_request)
    request_thread.start()
    try:
        assert provider_started.wait(timeout=2)
        asyncio.run(unrelated_write())
    finally:
        release_provider.set()
        request_thread.join(timeout=5)
        asyncio.run(fast_writer_db.dispose())

    response = response_holder["response"]
    assert getattr(response, "status_code", None) == 200


def test_send_code_cancellation_compensates_only_reserved_row(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.auth.router as auth_router

    email = "cancelled-send@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    now_ms = asyncio.run(_db_now_seconds(migrated_database)) * 1000
    asyncio.run(
        _seed_verification_row(
            migrated_database,
            email=email,
            code_digest="existing-login-digest",
            purpose="login",
            expires_at=now_ms + 60_000,
            created_at=now_ms - 5_000,
        )
    )

    async def cancel_sender(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(auth_router, "send_verification_code", cancel_sender)

    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        client.post("/auth/send-code", json={"email": email})

    rows = asyncio.run(_get_verification_rows(migrated_database, email))
    assert len(rows) == 1
    assert rows[0]["code_digest"] == "existing-login-digest"


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


def test_pending_purge_normal_cookie_is_blocked_from_non_recovery_api(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="pending-normal@example.com",
        )
    )

    deletion_response = client.post("/api/account/deletion", cookies=identity.cookie)
    assert deletion_response.status_code == 200

    user_row = asyncio.run(_get_user_row(migrated_database, identity.email))
    pending_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=identity.tenant_id,
        email=identity.email,
        auth_epoch=int(user_row["auth_epoch"]),
    )
    blocked = client.get("/api/sessions", cookies=pending_cookie)

    assert blocked.status_code == 403
    assert blocked.json() == {"detail": "Account pending deletion"}


def test_account_deletion_boundary_recent_auth_and_active_run_contracts(
    client: TestClient,
    migrated_database: Database,
):
    boundary_identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="boundary-delete@example.com",
        )
    )
    now_seconds = asyncio.run(_db_now_seconds(migrated_database))
    boundary_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=boundary_identity.tenant_id,
        email=boundary_identity.email,
        auth_epoch=boundary_identity.auth_epoch,
        issued_at=now_seconds - 300,
        expires_at=now_seconds + 3600,
    )
    stale_identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="stale-delete@example.com",
        )
    )
    stale_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=stale_identity.tenant_id,
        email=stale_identity.email,
        auth_epoch=stale_identity.auth_epoch,
        issued_at=now_seconds - 301,
        expires_at=now_seconds + 3600,
    )

    boundary = client.post("/api/account/deletion", cookies=boundary_cookie)
    stale = client.post("/api/account/deletion", cookies=stale_cookie)

    assert boundary.status_code == 200
    assert stale.status_code == 401
    assert stale.json() == {"detail": "Recent authentication required"}

    active_identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="active-run-delete@example.com",
        )
    )
    now_ms = asyncio.run(_db_now_seconds(migrated_database)) * 1000
    asyncio.run(
        _seed_active_run(
            migrated_database,
            tenant_id=active_identity.tenant_id,
            workspace_id=str(active_identity.workspace_id),
            lease_expires_at=now_ms + 60_000,
        )
    )

    active_run = client.post("/api/account/deletion", cookies=active_identity.cookie)

    assert active_run.status_code == 409
    assert active_run.json() == {
        "detail": {
            "code": "ACTIVE_RUNS",
            "message": "Active runs must finish first",
        }
    }


def test_account_deletion_with_retention_zero_stays_pending_until_worker_purges(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="retention-zero@example.com",
        )
    )
    previous_retention = client.app.state.settings.deletion.retention_days
    client.app.state.settings.deletion.retention_days = 0
    try:
        response = client.post("/api/account/deletion", cookies=identity.cookie)
    finally:
        client.app.state.settings.deletion.retention_days = previous_retention

    state = asyncio.run(_get_deletion_state(migrated_database, identity.tenant_id))

    assert response.status_code == 200
    assert state is not None
    assert response.json()["purge_after"] == response.json()["requested_at"]
    assert state["user_status"] == "pending_purge"
    assert state["job_status"] == "scheduled"
    assert state["job_id"] == response.json()["job_id"]


def test_duplicate_account_deletion_returns_existing_schedule_for_recent_pending_token(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="duplicate-delete-route@example.com",
        )
    )

    first = client.post("/api/account/deletion", cookies=identity.cookie)
    user_row = asyncio.run(_get_user_row(migrated_database, identity.email))
    now_seconds = asyncio.run(_db_now_seconds(migrated_database))
    pending_cookie = _make_cookie(
        TEST_JWT_SIGNING_KEY,
        user_id=identity.tenant_id,
        email=identity.email,
        auth_epoch=int(user_row["auth_epoch"]),
        issued_at=now_seconds,
        expires_at=now_seconds + 3600,
    )

    second = client.post("/api/account/deletion", cookies=pending_cookie)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_deletion_request_clears_normal_cookie_and_old_jwt_keeps_pending_semantics(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="old-cookie-pending@example.com",
        )
    )

    deletion_response = client.post("/api/account/deletion", cookies=identity.cookie)
    blocked = client.get("/api/sessions", cookies=identity.cookie)

    assert deletion_response.status_code == 200
    assert any(
        cookie.startswith("token=") and "Max-Age=0" in cookie
        for cookie in deletion_response.headers.get_list("set-cookie")
    )
    assert blocked.status_code == 403
    assert blocked.json() == {"detail": "Account pending deletion"}


def test_deletion_request_rejects_reusing_same_old_normal_cookie(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="old-cookie-reuse@example.com",
        )
    )

    first = client.post("/api/account/deletion", cookies=identity.cookie)
    second = client.post("/api/account/deletion", cookies=identity.cookie)

    assert first.status_code == 200
    assert second.status_code in {401, 403}
    assert second.status_code != 200


def test_deletion_recovery_verify_sets_recovery_cookie_without_normal_session(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="pending-recovery@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    send_response = client.post(
        "/auth/deletion-recovery/send-code",
        json={"email": identity.email},
    )
    verify_response = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "112233"},
    )

    assert send_response.status_code == 200
    assert verify_response.status_code == 200
    cookies = verify_response.headers.get_list("set-cookie")
    assert any(cookie.startswith("recovery_token=") for cookie in cookies)
    assert all(not cookie.startswith("token=") for cookie in cookies)


def test_deletion_recovery_unknown_email_is_enumeration_safe_and_does_not_send(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.auth.router as auth_router

    sent: list[tuple[str, str]] = []

    async def record_send(_settings, email: str, code: str) -> None:
        sent.append((email, code))

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    monkeypatch.setattr(auth_router, "send_verification_code", record_send)

    response = client.post("/auth/deletion-recovery/send-code", json={"email": "unknown@example.com"})
    rows = asyncio.run(_get_verification_rows(migrated_database, "unknown@example.com"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert rows == []
    assert sent == []


def test_deletion_status_with_recovery_token_returns_limited_view(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="status-view@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200
    assert (
        client.post("/auth/deletion-recovery/verify", json={"email": identity.email, "code": "112233"}).status_code
        == 200
    )

    recovery_token = client.cookies.get("recovery_token")
    assert recovery_token is not None
    client.cookies.clear()
    status_response = client.get("/api/account/deletion", cookies={"recovery_token": recovery_token})

    assert status_response.status_code == 200
    assert status_response.json() == {
        "status": "pending_purge",
        "purge_after": scheduled.json()["purge_after"],
    }


def test_deletion_status_rejects_normal_session_and_requires_recovery_token(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="normal-status-rejected@example.com",
        )
    )

    response = client.get("/api/account/deletion", cookies=identity.cookie)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_deletion_status_and_recover_accept_recovery_bearer_token(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-bearer@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200
    verify_response = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "112233"},
    )
    assert verify_response.status_code == 200
    recovery_token = client.cookies.get("recovery_token")
    csrf_token = _latest_cookie_value(client, "csrf_token")
    assert recovery_token is not None
    assert csrf_token is not None

    status_response = client.get(
        "/api/account/deletion",
        headers={"Authorization": f"Bearer {recovery_token}"},
    )
    recover_response = client.post(
        "/api/account/deletion/recover",
        headers={
            "Authorization": f"Bearer {recovery_token}",
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_token,
        },
        cookies={"csrf_token": csrf_token},
    )

    user_row = asyncio.run(_get_user_row(migrated_database, identity.email))
    deletion_state = asyncio.run(_get_deletion_state(migrated_database, identity.tenant_id))

    assert status_response.status_code == 200
    assert status_response.json() == {
        "status": "pending_purge",
        "purge_after": scheduled.json()["purge_after"],
    }
    assert recover_response.status_code == 200
    assert recover_response.json() == {"ok": True}
    assert user_row["status"] == "active"
    assert deletion_state is not None
    assert deletion_state["job_id"] is None
    assert any(
        cookie.startswith("recovery_token=") and "Max-Age=0" in cookie
        for cookie in recover_response.headers.get_list("set-cookie")
    )


def test_deletion_recovery_success_requires_fresh_normal_login(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recover-needs-relogin@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200
    verify_response = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "112233"},
    )
    assert verify_response.status_code == 200
    recovery_token = client.cookies.get("recovery_token")
    csrf_token = _latest_cookie_value(client, "csrf_token")
    assert recovery_token is not None
    assert csrf_token is not None

    recover_response = client.post(
        "/api/account/deletion/recover",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_token,
        },
        cookies={"recovery_token": recovery_token, "csrf_token": csrf_token},
    )

    assert recover_response.status_code == 200
    assert all(
        not cookie.startswith("token=") or "Max-Age=0" in cookie
        for cookie in recover_response.headers.get_list("set-cookie")
    )

    current_client_access = client.get("/api/sessions")
    stale_token_access = client.get("/api/sessions", cookies=identity.cookie)

    assert current_client_access.status_code == 401
    assert stale_token_access.status_code == 401

    client.app.state.auth_forced_code = "654321"
    assert client.post("/auth/send-code", json={"email": identity.email}).status_code == 200
    relogin = client.post(
        "/auth/verify",
        json={"email": identity.email, "code": "654321"},
    )
    restored_access = client.get("/api/sessions")

    assert relogin.status_code == 200
    assert any(cookie.startswith("token=") for cookie in relogin.headers.get_list("set-cookie"))
    assert restored_access.status_code == 200


def test_deletion_status_rejects_malformed_recovery_claim_types(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="malformed-recovery@example.com",
        )
    )
    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    now_seconds = asyncio.run(_db_now_seconds(migrated_database))
    token = jwt.encode(
        {
            "sub": identity.tenant_id,
            "email": identity.email,
            "aud": "multiclaw-deletion-recovery",
            "purpose": "deletion_recovery",
            "job_id": scheduled.json()["job_id"],
            "iat": "1700000000",
            "exp": now_seconds + 600,
        },
        TEST_JWT_SIGNING_KEY,
        algorithm="HS256",
    )

    response = client.get("/api/account/deletion", cookies={"recovery_token": token})

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_deletion_status_returns_410_once_recovery_window_is_closed(
    client: TestClient,
    migrated_database: Database,
):
    import multiclaw.auth.router as auth_router

    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="closed-window@example.com",
        )
    )
    previous_retention = client.app.state.settings.deletion.retention_days
    client.app.state.settings.deletion.retention_days = 0
    try:
        scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    finally:
        client.app.state.settings.deletion.retention_days = previous_retention

    assert scheduled.status_code == 200
    now_seconds = asyncio.run(_db_now_seconds(migrated_database))
    token = auth_router._make_deletion_recovery_jwt(
        user_id=identity.tenant_id,
        email=identity.email,
        job_id=scheduled.json()["job_id"],
        signing_key=TEST_JWT_SIGNING_KEY.encode("utf-8"),
        issued_at=now_seconds,
    )

    response = client.get("/api/account/deletion", cookies={"recovery_token": token})

    assert response.status_code == 410
    assert response.json() == {"detail": "Deletion recovery window expired"}


def test_deletion_recovery_send_code_nonpending_email_is_enumeration_safe(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.auth.router as auth_router

    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="active-no-recovery@example.com",
        )
    )
    sent: list[tuple[str, str]] = []

    async def record_send(_settings, email: str, code: str) -> None:
        sent.append((email, code))

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    monkeypatch.setattr(auth_router, "send_verification_code", record_send)

    response = client.post("/auth/deletion-recovery/send-code", json={"email": identity.email})
    rows = asyncio.run(_get_verification_rows(migrated_database, identity.email))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert rows == []
    assert sent == []


def test_deletion_recovery_send_code_rate_limits_real_pending_scheduled_email(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-quota@example.com",
        )
    )
    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    now_ms = asyncio.run(_db_now_seconds(migrated_database)) * 1000
    for offset in range(MAX_SENDS_PER_DAY):
        asyncio.run(
            _seed_verification_row(
                migrated_database,
                email=identity.email,
                code_digest=f"recovery-quota-{offset}",
                purpose="deletion_recovery",
                expires_at=now_ms + 60_000 + offset,
                created_at=now_ms - 1_000 + offset,
            )
        )

    response = client.post("/auth/deletion-recovery/send-code", json={"email": identity.email})
    rows = asyncio.run(_get_verification_rows(migrated_database, identity.email))

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many attempts, please try again tomorrow"}
    assert [row["purpose"] for row in rows].count("deletion_recovery") == MAX_SENDS_PER_DAY


def test_deletion_recovery_send_code_skips_expired_pending_window(
    client: TestClient,
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.auth.router as auth_router

    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="expired-send-window@example.com",
        )
    )
    previous_retention = client.app.state.settings.deletion.retention_days
    client.app.state.settings.deletion.retention_days = 0
    try:
        scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    finally:
        client.app.state.settings.deletion.retention_days = previous_retention

    assert scheduled.status_code == 200
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = False
    sent: list[tuple[str, str]] = []

    async def record_send(_settings, email: str, code: str) -> None:
        sent.append((email, code))

    monkeypatch.setattr(auth_router, "send_verification_code", record_send)

    response = client.post("/auth/deletion-recovery/send-code", json={"email": identity.email})
    rows = asyncio.run(_get_verification_rows(migrated_database, identity.email))

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert rows == []
    assert sent == []


def test_deletion_recovery_verify_wrong_code_rejects_without_setting_cookie(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="wrong-recovery-code@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200

    response = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "000000"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired verification code"}
    assert "recovery_token" not in client.cookies


def test_login_verify_accepts_correct_code_after_four_failures(
    client: TestClient,
    migrated_database: Database,
):
    email = "login-four-failures@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"

    assert client.post("/auth/send-code", json={"email": email}).status_code == 200

    wrong_responses = [
        client.post("/auth/verify", json={"email": email, "code": "000000"})
        for _ in range(4)
    ]
    success = client.post("/auth/verify", json={"email": email, "code": "112233"})
    me_response = client.get("/auth/me")
    rows = asyncio.run(_get_verification_rows(migrated_database, email))

    assert [response.status_code for response in wrong_responses] == [401, 401, 401, 401]
    assert success.status_code == 200
    assert me_response.status_code == 200
    assert me_response.json()["email"] == email
    assert len(rows) == 1
    assert rows[0]["used_at"] is not None


def test_login_verify_locks_latest_code_after_five_failures_until_new_issue(
    client: TestClient,
    migrated_database: Database,
):
    email = "login-lockout@example.com"
    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True

    client.app.state.auth_forced_code = "111111"
    assert client.post("/auth/send-code", json={"email": email}).status_code == 200
    client.app.state.auth_forced_code = "222222"
    assert client.post("/auth/send-code", json={"email": email}).status_code == 200

    wrong_responses = [
        client.post("/auth/verify", json={"email": email, "code": "000000"})
        for _ in range(5)
    ]
    latest_locked = client.post("/auth/verify", json={"email": email, "code": "222222"})
    old_still_invalid = client.post("/auth/verify", json={"email": email, "code": "111111"})
    client.app.state.auth_forced_code = "333333"
    reset_send = client.post("/auth/send-code", json={"email": email})
    reset_success = client.post("/auth/verify", json={"email": email, "code": "333333"})
    client.cookies.clear()
    locked_code_after_reset = client.post("/auth/verify", json={"email": email, "code": "222222"})
    old_code_after_reset = client.post("/auth/verify", json={"email": email, "code": "111111"})
    rows = asyncio.run(_get_verification_rows(migrated_database, email))

    assert [response.status_code for response in wrong_responses] == [401, 401, 401, 401, 401]
    assert latest_locked.status_code == 401
    assert old_still_invalid.status_code == 401
    assert reset_send.status_code == 200
    assert reset_success.status_code == 200
    assert locked_code_after_reset.status_code == 401
    assert old_code_after_reset.status_code == 401
    assert [row["used_at"] is not None for row in rows] == [False, True, True]


def test_deletion_recovery_verify_sets_secure_cookie_flags_in_production(
    migrated_database: Database,
):
    import multiclaw.server as server

    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-cookie-flags@example.com",
        )
    )

    with TestClient(server.app, base_url="https://app.example") as client:
        scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
        assert scheduled.status_code == 200

        client.app.state.settings.email.provider = "resend"
        client.app.state.settings.resend.mock = True
        client.app.state.auth_forced_code = "112233"
        assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200
        response = client.post(
            "/auth/deletion-recovery/verify",
            json={"email": identity.email, "code": "112233"},
        )

    cookies = response.headers.get_list("set-cookie")
    recovery_cookie = next(value for value in cookies if value.startswith("recovery_token="))
    csrf_cookie = next(value for value in cookies if value.startswith("csrf_token="))
    assert "Secure" in recovery_cookie
    assert "SameSite=lax" in recovery_cookie
    assert "HttpOnly" in recovery_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie


def test_deletion_recovery_recover_returns_410_once_window_is_closed(
    client: TestClient,
    migrated_database: Database,
):
    import multiclaw.auth.router as auth_router

    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recover-window-closed@example.com",
        )
    )
    previous_retention = client.app.state.settings.deletion.retention_days
    client.app.state.settings.deletion.retention_days = 0
    try:
        scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    finally:
        client.app.state.settings.deletion.retention_days = previous_retention

    assert scheduled.status_code == 200
    now_seconds = asyncio.run(_db_now_seconds(migrated_database))
    token = auth_router._make_deletion_recovery_jwt(
        user_id=identity.tenant_id,
        email=identity.email,
        job_id=scheduled.json()["job_id"],
        signing_key=TEST_JWT_SIGNING_KEY.encode("utf-8"),
        issued_at=now_seconds,
    )

    response = client.post("/api/account/deletion/recover", cookies={"recovery_token": token})

    assert response.status_code == 410
    assert response.json() == {"detail": "Deletion recovery window expired"}


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


def test_deletion_recovery_verify_accepts_correct_code_after_four_failures(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-four-failures@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "112233"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200

    wrong_responses = [
        client.post("/auth/deletion-recovery/verify", json={"email": identity.email, "code": "000000"})
        for _ in range(4)
    ]
    success = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "112233"},
    )
    rows = asyncio.run(_get_verification_rows(migrated_database, identity.email))

    assert [response.status_code for response in wrong_responses] == [401, 401, 401, 401]
    assert success.status_code == 200
    assert client.cookies.get("recovery_token") is not None
    assert rows[-1]["used_at"] is not None


def test_deletion_recovery_verify_locks_latest_code_after_five_failures_until_new_issue(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="recovery-lockout@example.com",
        )
    )

    scheduled = client.post("/api/account/deletion", cookies=identity.cookie)
    assert scheduled.status_code == 200

    client.app.state.settings.email.provider = "resend"
    client.app.state.settings.resend.mock = True
    client.app.state.auth_forced_code = "111111"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200
    client.app.state.auth_forced_code = "222222"
    assert client.post("/auth/deletion-recovery/send-code", json={"email": identity.email}).status_code == 200

    wrong_responses = [
        client.post("/auth/deletion-recovery/verify", json={"email": identity.email, "code": "000000"})
        for _ in range(5)
    ]
    latest_locked = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "222222"},
    )
    old_still_invalid = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "111111"},
    )
    client.app.state.auth_forced_code = "333333"
    reset_send = client.post("/auth/deletion-recovery/send-code", json={"email": identity.email})
    reset_success = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "333333"},
    )
    recovery_token = client.cookies.get("recovery_token")
    client.cookies.clear()
    locked_code_after_reset = client.post(
        "/auth/deletion-recovery/verify",
        json={"email": identity.email, "code": "222222"},
    )
    rows = asyncio.run(_get_verification_rows(migrated_database, identity.email))

    assert [response.status_code for response in wrong_responses] == [401, 401, 401, 401, 401]
    assert latest_locked.status_code == 401
    assert old_still_invalid.status_code == 401
    assert reset_send.status_code == 200
    assert reset_success.status_code == 200
    assert recovery_token is not None
    assert locked_code_after_reset.status_code == 401
    assert [row["used_at"] is not None for row in rows if row["purpose"] == "deletion_recovery"] == [
        False,
        True,
        True,
    ]


@pytest.mark.asyncio
async def test_verification_codes_ignore_wrong_purpose_and_are_consumed_once_atomically(
    migrated_database: Database,
) -> None:
    recovery_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email="atomic@example.com",
        purpose="deletion_recovery",
        forced_code="111111",
    ).code_digest
    login_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email="atomic@example.com",
        purpose="login",
        forced_code="222222",
    ).code_digest
    async with AuthUnitOfWork(migrated_database) as uow:
        await uow.conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email="atomic@example.com",
                code_digest=recovery_digest,
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
                code_digest=login_digest,
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
            code_digest=recovery_digest,
        ) is None
        first = await uow.verification_codes.consume_latest_code(
            email="atomic@example.com",
            purpose="login",
            code_digest=login_digest,
        )
        second = await uow.verification_codes.consume_latest_code(
            email="atomic@example.com",
            purpose="login",
            code_digest=login_digest,
        )

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_verification_code_failures_are_purpose_isolated_and_lock_only_matching_latest_code(
    migrated_database: Database,
) -> None:
    email = "purpose-isolation@example.com"
    login_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email=email,
        purpose="login",
        forced_code="111111",
    ).code_digest
    login_wrong_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email=email,
        purpose="login",
        forced_code="000000",
    ).code_digest
    recovery_digest = issue_verification_code(
        TEST_JWT_SIGNING_KEY.encode("utf-8"),
        email=email,
        purpose="deletion_recovery",
        forced_code="222222",
    ).code_digest

    async with AuthUnitOfWork(migrated_database) as uow:
        now_ms = await uow.verification_codes.db_now_ms()
        await uow.conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email=email,
                code_digest=login_digest,
                purpose="login",
                expires_at=now_ms + 60_000,
                used_at=None,
                created_at=now_ms,
            )
        )
        await uow.conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email=email,
                code_digest=recovery_digest,
                purpose="deletion_recovery",
                expires_at=now_ms + 60_000,
                used_at=None,
                created_at=now_ms + 1,
            )
        )

    async with AuthUnitOfWork(migrated_database) as uow:
        for _ in range(5):
            assert (
                await uow.verification_codes.consume_latest_code(
                    email=email,
                    purpose="login",
                    code_digest=login_wrong_digest,
                )
            ) is None
        recovery = await uow.verification_codes.consume_latest_code(
            email=email,
            purpose="deletion_recovery",
            code_digest=recovery_digest,
        )
        recovery_second = await uow.verification_codes.consume_latest_code(
            email=email,
            purpose="deletion_recovery",
            code_digest=recovery_digest,
        )
        locked_login = await uow.verification_codes.consume_latest_code(
            email=email,
            purpose="login",
            code_digest=login_digest,
        )

    assert recovery is not None
    assert recovery_second is None
    assert locked_login is None


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
    assert client.post("/auth/verify", json={"email": email, "code": "111111"}).status_code == 401


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


@pytest.mark.parametrize("status", ["disabled"])
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


def test_seeded_pending_purge_user_returns_403(
    client: TestClient,
    migrated_database: Database,
):
    identity = asyncio.run(
        _seed_identity(
            migrated_database,
            TEST_JWT_SIGNING_KEY,
            email="seeded-pending@example.com",
            status="pending_purge",
        )
    )

    response = client.get("/api/sessions", cookies=identity.cookie)

    assert response.status_code == 403
    assert response.json() == {"detail": "Account pending deletion"}


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
