# Email Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add email-based login via Brevo verification codes with JWT auth, protecting all existing endpoints.

**Architecture:** New `auth` package with models, Brevo sender, SQLite store, JWT middleware, and FastAPI router. JWT secret persisted in DB. Existing session store gains `user_id` column. Frontend gets a two-step login card (email → code) in the existing single-page HTML.

**Tech Stack:** FastAPI, pyjwt, httpx, aiosqlite, vanilla HTML/CSS/JS

---

### Task 1: Add pyjwt dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pyjwt**

In `pyproject.toml`, add `"pyjwt>=2.9",` to the dependencies list (after `"aiosqlite>=0.20",`):

```toml
    "pyjwt>=2.9",
```

- [ ] **Step 2: Install**

Run: `pip install pyjwt>=2.9`

---

### Task 2: Add AuthSettings and BrevoSettings

**Files:**
- Modify: `src/multiclaw/config/settings.py`

- [ ] **Step 1: Add settings classes**

After the `SkillSettings` class (line ~66), add:

```python
class AuthSettings(BaseModel):
    jwt_secret: str = ""


class BrevoSettings(BaseModel):
    api_key: str = ""
    sender_email: str = ""
    sender_name: str = "MultiClaw"
```

- [ ] **Step 2: Add fields to Settings class**

After `skill: SkillSettings = Field(default_factory=SkillSettings)`, add:

```python
auth: AuthSettings = Field(default_factory=AuthSettings)
brevo: BrevoSettings = Field(default_factory=BrevoSettings)
```

- [ ] **Step 3: Add TOML parsing**

In `_build_toml_kwargs`, after the `if "skills" in data:` block, add:

```python
if "auth" in data:
    result["auth"] = data["auth"]
if "brevo" in data:
    result["brevo"] = data["brevo"]
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_config.py -v`
Expected: PASS

---

### Task 3: Create auth models

**Files:**
- Create: `src/multiclaw/auth/__init__.py`
- Create: `src/multiclaw/auth/models.py`

- [ ] **Step 1: __init__.py**

```python
```

(empty file)

- [ ] **Step 2: models.py**

```python
from datetime import datetime, timezone
import uuid

from pydantic import BaseModel, Field


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationCode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    code: str
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    used: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SendCodeRequest(BaseModel):
    email: str


class VerifyRequest(BaseModel):
    email: str
    code: str


class AuthResponse(BaseModel):
    ok: bool = True


class MeResponse(BaseModel):
    email: str | None = None
    user_id: str | None = None
```

---

### Task 4: Create Brevo email sender

**Files:**
- Create: `src/multiclaw/auth/brevo.py`

- [ ] **Step 1: brevo.py**

```python
import httpx

from multiclaw.config import Settings

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


async def send_verification_code(settings: Settings, to_email: str, code: str) -> None:
    brevo = settings.brevo
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            BREVO_API_URL,
            headers={
                "api-key": brevo.api_key,
                "Content-Type": "application/json",
            },
            json={
                "sender": {"name": brevo.sender_name, "email": brevo.sender_email},
                "to": [{"email": to_email}],
                "subject": "MultiClaw Verification Code",
                "htmlContent": (
                    f'<div style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:24px">'
                    f'<h2 style="color:#333">Verification Code</h2>'
                    f'<p style="font-size:16px;color:#555">Your code is:</p>'
                    f'<div style="font-size:32px;font-weight:bold;letter-spacing:6px;'
                    f'padding:16px 24px;background:#f5f5f5;border-radius:8px;text-align:center;margin:16px 0">'
                    f'{code}</div>'
                    f'<p style="font-size:13px;color:#999">Expires in 15 minutes.</p>'
                    f'</div>'
                ),
            },
        )
        resp.raise_for_status()
```

---

### Task 5: Create auth store

**Files:**
- Create: `src/multiclaw/auth/store.py`

- [ ] **Step 1: store.py**

```python
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from multiclaw.auth.models import User, VerificationCode

CODE_EXPIRY_MINUTES = 15
MAX_SENDS_PER_DAY = 3


class AuthStore:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._db: aiosqlite.Connection | None = None
        self.jwt_secret: str = ""

    async def initialize(self) -> None:
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Auth tables
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_codes (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verification_email
            ON verification_codes(email, created_at DESC)
            """
        )
        # config table for jwt_secret persistence
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Load or generate jwt_secret
        cursor = await self._db.execute(
            "SELECT value FROM auth_config WHERE key = 'jwt_secret'"
        )
        row = await cursor.fetchone()
        if row:
            self.jwt_secret = row["value"]
        else:
            self.jwt_secret = secrets.token_hex(32)
            await self._db.execute(
                "INSERT INTO auth_config (key, value) VALUES ('jwt_secret', ?)",
                (self.jwt_secret,),
            )

        await self._migrate_sessions()
        await self._db.commit()

    async def _migrate_sessions(self) -> None:
        assert self._db is not None
        cursor = await self._db.execute("PRAGMA table_info(chat_sessions)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "user_id" not in columns:
            await self._db.execute(
                "ALTER TABLE chat_sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
            )

    async def create_code(self, email: str) -> VerificationCode:
        import random

        db = await self._ensure_db()
        code_str = f"{random.randint(0, 999999):06d}"
        now = datetime.now(timezone.utc)
        vc = VerificationCode(
            email=email,
            code=code_str,
            expires_at=now + timedelta(minutes=CODE_EXPIRY_MINUTES),
            created_at=now,
        )
        await db.execute(
            """
            INSERT INTO verification_codes (id, email, code, expires_at, used, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (vc.id, vc.email, vc.code, vc.expires_at.isoformat(), now.isoformat()),
        )
        await db.commit()
        return vc

    async def count_recent_sends(self, email: str) -> int:
        db = await self._ensure_db()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM verification_codes WHERE email = ? AND created_at > ?",
            (email, cutoff.isoformat()),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def find_latest_unused_code(self, email: str) -> dict | None:
        db = await self._ensure_db()
        cursor = await db.execute(
            """
            SELECT * FROM verification_codes
            WHERE email = ? AND used = 0 AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (email, datetime.now(timezone.utc).isoformat()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def mark_code_used(self, code_id: str) -> None:
        db = await self._ensure_db()
        await db.execute(
            "UPDATE verification_codes SET used = 1 WHERE id = ?",
            (code_id,),
        )
        await db.commit()

    async def get_or_create_user(self, email: str) -> User:
        db = await self._ensure_db()
        cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
        if row:
            return User(
                id=row["id"],
                email=row["email"],
                created_at=row["created_at"],
            )
        user = User(email=email)
        await db.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user.id, user.email, user.created_at.isoformat()),
        )
        await db.commit()
        return user

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db
```

---

### Task 6: Create auth middleware

**Files:**
- Create: `src/multiclaw/auth/middleware.py`

- [ ] **Step 1: middleware.py**

```python
import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


PUBLIC_PREFIXES = ("/auth/",)
PUBLIC_EXACT = {"/multiclaw.png"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        store = request.app.state.auth_store
        token = request.cookies.get("token")
        user = None
        if token:
            try:
                payload = jwt.decode(token, store.jwt_secret, algorithms=["HS256"])
                user = {"id": payload["sub"], "email": payload["email"]}
            except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
                pass

        request.state.user = user

        path = request.url.path
        # Public paths
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT:
            return await call_next(request)

        # HTML index page is always served (frontend handles auth state)
        if path == "/":
            return await call_next(request)

        # All other routes require auth
        if not user:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)


def require_auth(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user
```

---

### Task 7: Create auth router

**Files:**
- Create: `src/multiclaw/auth/router.py`

- [ ] **Step 1: router.py**

```python
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request, Response

from multiclaw.auth.brevo import send_verification_code
from multiclaw.auth.models import (
    AuthResponse,
    MeResponse,
    SendCodeRequest,
    VerifyRequest,
)
from multiclaw.auth.store import MAX_SENDS_PER_DAY, AuthStore

router = APIRouter(prefix="/auth")


def _get_store(request: Request) -> AuthStore:
    return request.app.state.auth_store


def _get_settings(request: Request):
    return request.app.state.settings


def _make_jwt(user_id: str, email: str, secret: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(days=10),
        },
        secret,
        algorithm="HS256",
    )


@router.post("/send-code", response_model=AuthResponse)
async def send_code(body: SendCodeRequest, request: Request):
    email = body.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=422, detail="Please enter a valid email")

    store = _get_store(request)
    recent = await store.count_recent_sends(email)
    if recent >= MAX_SENDS_PER_DAY:
        raise HTTPException(
            status_code=429, detail="Too many attempts, please try again tomorrow"
        )

    code = await store.create_code(email)
    settings = _get_settings(request)
    try:
        await send_verification_code(settings, email, code.code)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Failed to send email, please try again later"
        )

    return AuthResponse()


@router.post("/verify", response_model=AuthResponse)
async def verify(body: VerifyRequest, request: Request, response: Response):
    email = body.email.strip().lower()
    code = body.code.strip()

    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="Invalid code format")

    store = _get_store(request)
    vc = await store.find_latest_unused_code(email)
    if vc is None or vc["code"] != code:
        raise HTTPException(
            status_code=401, detail="Invalid or expired verification code"
        )

    await store.mark_code_used(vc["id"])
    user = await store.get_or_create_user(email)

    token = _make_jwt(user.id, user.email, store.jwt_secret)
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=864000,
        path="/",
    )

    return AuthResponse()


@router.post("/logout", response_model=AuthResponse)
async def logout(response: Response):
    response.delete_cookie(key="token", path="/")
    return AuthResponse()


@router.get("/me", response_model=MeResponse)
async def me(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return MeResponse()
    return MeResponse(email=user["email"], user_id=user["id"])
```

---

### Task 8: Register auth in server.py

**Files:**
- Modify: `src/multiclaw/server.py`

- [ ] **Step 1: Add imports**

After the existing multiclaw imports (~line 100), add:

```python
from multiclaw.auth.store import AuthStore
from multiclaw.auth.middleware import AuthMiddleware, require_auth
from multiclaw.auth.router import router as auth_router
```

- [ ] **Step 2: Update lifespan to initialize auth**

Replace the lifespan function:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    agent = create_agent()
    auth_store = AuthStore(agent.settings.database.path)
    await auth_store.initialize()
    app.state.auth_store = auth_store
    app.state.settings = agent.settings
    yield
```

- [ ] **Step 3: Add middleware and router**

After `app = FastAPI(title="MultiClaw", lifespan=lifespan)`:

```python
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
```

The middleware reads `request.app.state.auth_store` at request time, which is safe because the lifespan runs before any request is served, guaranteeing `app.state.auth_store` exists.

- [ ] **Step 4: Add import for Depends**

Update the FastAPI import at top:

```python
from fastapi import Depends, FastAPI, HTTPException
```

- [ ] **Step 5: Add require_auth to protected endpoints**

For `/chat` endpoint — change signature to:

```python
@app.post("/chat")
async def chat(req: ChatRequest, request: Request, user: dict = Depends(require_auth)):
```

For `/sessions` (list):

```python
@app.get("/sessions")
async def list_sessions(include_archived: bool = False, user: dict = Depends(require_auth)):
    sessions = await agent.session_store.list_sessions(
        include_archived=include_archived, user_id=user["id"]
    )
    return [session.model_dump(mode="json") for session in sessions]
```

For `/sessions` (create):

```python
@app.post("/sessions")
async def create_session(req: SessionCreateRequest, user: dict = Depends(require_auth)):
    session = await agent.session_store.create(title=req.title, user_id=user["id"])
    return session.model_dump(mode="json")
```

For `/sessions/{session_id}` (rename):

```python
@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.rename(session_id, req.title)
    return session.model_dump(mode="json")
```

For `/sessions/{session_id}/archive`:

```python
@app.post("/sessions/{session_id}/archive")
async def archive_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.archive(session_id)
    return session.model_dump(mode="json")
```

For `/sessions/{session_id}/restore`:

```python
@app.post("/sessions/{session_id}/restore")
async def restore_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.restore(session_id)
    return session.model_dump(mode="json")
```

For `/sessions/{session_id}` (delete):

```python
@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    await agent.session_store.delete(session_id)
    return {"ok": True}
```

For `/sessions/{session_id}/messages`:

```python
@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = 50, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    return await agent.session_store.get_messages(session_id, limit)
```

For `/approve` — needs no ownership check (approval is request-scoped), just auth:

```python
@app.post("/approve")
async def approve(req: ApproveRequest, user: dict = Depends(require_auth)):
    ok = agent.scheduler.resolve_approval(req.request_id, req.approved)
    return {"ok": ok}
```

---

### Task 9: Add user_id to session model and store

**Files:**
- Modify: `src/multiclaw/session/models.py`
- Modify: `src/multiclaw/session/sqlite.py`

- [ ] **Step 1: Add user_id to ChatSession model**

In `models.py`, add `user_id` field after `status`:

```python
class ChatSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "New Chat"
    status: SessionStatus = SessionStatus.ACTIVE
    user_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 2: Update CREATE TABLE in sqlite.py initialize()**

Change the chat_sessions CREATE to include user_id:

```python
await self._db.execute(
    """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        user_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_message_at TEXT,
        metadata TEXT NOT NULL
    )
    """
)
```

(Note: the migration in AuthStore.initialize() runs first and adds the column via ALTER TABLE if needed.)

- [ ] **Step 3: Update create()**

```python
async def create(self, title: str = "New Chat", user_id: str = "") -> ChatSession:
    title = _validate_title(title)
    session = ChatSession(title=title, user_id=user_id)
    db = await self._ensure_db()
    await db.execute(
        """
        INSERT INTO chat_sessions (
            id, title, status, user_id, created_at, updated_at, last_message_at, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session.id,
            session.title,
            session.status.value,
            session.user_id,
            session.created_at.isoformat(),
            session.updated_at.isoformat(),
            None,
            json.dumps(session.metadata),
        ),
    )
    await db.commit()
    return session
```

- [ ] **Step 4: Update list_sessions()**

```python
async def list_sessions(self, include_archived: bool = False, user_id: str | None = None) -> list[ChatSession]:
    db = await self._ensure_db()
    query = "SELECT * FROM chat_sessions WHERE 1=1"
    params: list[str] = []
    if not include_archived:
        query += " AND status = ?"
        params.append(SessionStatus.ACTIVE.value)
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    query += " ORDER BY COALESCE(last_message_at, created_at) DESC"
    cursor = await db.execute(query, tuple(params))
    rows = await cursor.fetchall()
    return [_row_to_session(row) for row in rows]
```

- [ ] **Step 5: Update _row_to_session**

```python
def _row_to_session(row: aiosqlite.Row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        title=row["title"],
        status=SessionStatus(row["status"]),
        user_id=row["user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row["last_message_at"],
        metadata=json.loads(row["metadata"]),
    )
```

---

### Task 10: Frontend login page

**Files:**
- Modify: `src/multiclaw/static/index.html`

- [ ] **Step 1: Add login overlay CSS**

Add before `</style>` (after line 576 or wherever the closing tag is):

```css
/* ---- login overlay ---- */
#login-overlay {
  position: fixed; inset: 0; z-index: 10000;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-base);
}
#login-overlay.hidden { display: none; }
#login-card {
  width: 380px; max-width: 90vw;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 32px;
  box-shadow: var(--shadow);
  text-align: center;
}
#login-card h2 {
  font-family: var(--font-display);
  font-size: 22px; font-weight: 500; margin-bottom: 4px;
  color: var(--text-primary); letter-spacing: -0.3px;
}
#login-card .login-sub {
  font-size: 11px; color: var(--text-tertiary);
  text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 22px;
}
#login-card label {
  display: block; text-align: left;
  font-size: 12px; color: var(--text-secondary);
  margin-bottom: 4px; font-weight: 500;
}
#login-card .login-input {
  width: 100%; padding: 11px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  font-size: 14px; font-family: var(--font-body);
  background: var(--bg-input); color: var(--text-primary);
  outline: none; transition: border-color 0.2s; margin-bottom: 12px;
  box-sizing: border-box;
}
#login-card .login-input:focus { border-color: var(--accent); }
#login-card .login-btn {
  width: 100%; padding: 11px; border: none; border-radius: var(--radius-sm);
  font-size: 14px; font-weight: 500; font-family: var(--font-body);
  cursor: pointer; background: var(--accent); color: #1a1a1c;
  transition: all 0.15s ease;
}
#login-card .login-btn:hover { filter: brightness(1.1); box-shadow: 0 2px 10px var(--accent-glow); }
#login-card .login-btn:disabled { opacity: 0.4; cursor: not-allowed; filter: none; box-shadow: none; }
#login-card .login-error {
  font-size: 12px; color: var(--danger); text-align: center;
  margin-top: 10px; min-height: 16px;
}
#login-card .login-back {
  font-size: 12px; color: var(--text-secondary); margin-top: 12px;
  text-decoration: underline; cursor: pointer; display: inline-block;
}
#login-card .login-back:hover { color: var(--accent); }
#login-card .code-row {
  display: flex; gap: 8px; justify-content: center; margin-bottom: 16px;
}
#login-card .code-digit {
  width: 44px; height: 54px; text-align: center;
  font-size: 24px; font-family: var(--font-mono);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-input); color: var(--text-primary);
  outline: none; transition: border-color 0.15s;
}
#login-card .code-digit:focus { border-color: var(--accent); }
#login-card .resend-wrap {
  font-size: 12px; color: var(--text-tertiary); margin-top: 12px;
}
#login-card .resend-wrap a {
  color: var(--accent); cursor: pointer; text-decoration: none;
}
#login-card .resend-wrap a.disabled {
  color: var(--text-tertiary); cursor: not-allowed; pointer-events: none;
}
#logout-btn {
  background: none; border: none; color: var(--text-secondary);
  cursor: pointer; font-size: 12px; font-family: var(--font-body);
  padding: 4px 8px; border-radius: 6px; transition: all 0.15s ease;
}
#logout-btn:hover { background: var(--danger-soft); color: var(--danger); }
```

- [ ] **Step 2: Add login overlay HTML**

Add before `<!-- SIDEBAR -->` comment inside `<div id="app">`:

```html
<!-- LOGIN OVERLAY -->
<div id="login-overlay">
  <div id="login-card">
    <h2>MultiClaw</h2>
    <div class="login-sub">Sign in to continue</div>

    <div id="login-step-1">
      <label for="login-email">Email</label>
      <input class="login-input" id="login-email" type="email" placeholder="hello@example.com" autocomplete="email">
      <button class="login-btn" id="login-send-btn" onclick="authSendCode()">Send Code</button>
      <div class="login-error" id="login-error-1"></div>
    </div>

    <div id="login-step-2" style="display:none">
      <label>Verification Code</label>
      <p style="font-size:13px;color:var(--text-secondary);margin:4px 0 12px">
        Sent to <strong id="login-email-display"></strong>
      </p>
      <div class="code-row" id="code-row">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="0">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="1">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="2">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="3">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="4">
        <input class="code-digit" maxlength="1" inputmode="numeric" pattern="[0-9]" data-idx="5">
      </div>
      <button class="login-btn" id="login-verify-btn" onclick="authVerify()">Verify</button>
      <div class="login-error" id="login-error-2"></div>
      <div class="resend-wrap" id="resend-wrap">
        <a id="resend-link" class="disabled" onclick="authResend()">Resend code (60s)</a>
      </div>
      <div class="login-back" onclick="authBackToEmail()">&larr; Use a different email</div>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Add logout button**

Inside `<div class="topbar-controls">` after the theme toggle:

```html
<button id="logout-btn" onclick="authLogout()" title="Sign out" style="display:none">Sign out</button>
```

- [ ] **Step 4: Add auth JavaScript**

Add before the closing `</script>` tag:

```javascript
// ---- auth ----
var authEmail = '';
var resendSeconds = 0;
var resendTimer = null;

async function checkAuth() {
  try {
    var res = await fetch('/auth/me');
    var data = await res.json();
    if (data.email) {
      document.getElementById('login-overlay').classList.add('hidden');
      document.getElementById('logout-btn').style.display = '';
      loadSessions();
    } else {
      document.getElementById('login-overlay').classList.remove('hidden');
      document.getElementById('logout-btn').style.display = 'none';
    }
  } catch(e) {
    document.getElementById('login-overlay').classList.remove('hidden');
    document.getElementById('logout-btn').style.display = 'none';
  }
}

function showLoginError(step, msg) {
  document.getElementById('login-error-' + step).textContent = msg;
}

async function authSendCode() {
  var emailInput = document.getElementById('login-email');
  var email = emailInput.value.trim();
  showLoginError(1, '');
  if (!email || email.indexOf('@') < 0) {
    showLoginError(1, 'Please enter a valid email'); return;
  }
  var btn = document.getElementById('login-send-btn');
  btn.disabled = true; btn.textContent = 'Sending...';
  try {
    var res = await fetch('/auth/send-code', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: email}),
    });
    if (!res.ok) {
      var err = await res.json();
      showLoginError(1, err.detail || 'Failed to send code');
      btn.disabled = false; btn.textContent = 'Send Code'; return;
    }
    authEmail = email;
    document.getElementById('login-email-display').textContent = email;
    document.getElementById('login-step-1').style.display = 'none';
    document.getElementById('login-step-2').style.display = '';
    showLoginError(2, '');
    startResendTimer();
    var firstDigit = document.querySelector('#code-row .code-digit');
    if (firstDigit) firstDigit.focus();
  } catch(e) {
    showLoginError(1, 'Network error, please try again');
  } finally {
    btn.disabled = false; btn.textContent = 'Send Code';
  }
}

function startResendTimer() {
  resendSeconds = 60;
  updateResendLink();
  if (resendTimer) clearInterval(resendTimer);
  resendTimer = setInterval(function() {
    resendSeconds--;
    updateResendLink();
    if (resendSeconds <= 0) { clearInterval(resendTimer); resendTimer = null; }
  }, 1000);
}

function updateResendLink() {
  var link = document.getElementById('resend-link');
  if (resendSeconds > 0) {
    link.textContent = 'Resend code (' + resendSeconds + 's)';
    link.classList.add('disabled');
  } else {
    link.textContent = 'Resend code';
    link.classList.remove('disabled');
  }
}

async function authResend() {
  if (resendSeconds > 0) return;
  showLoginError(2, '');
  try {
    var res = await fetch('/auth/send-code', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: authEmail}),
    });
    if (!res.ok) {
      var err = await res.json();
      showLoginError(2, err.detail || 'Failed to resend code'); return;
    }
    startResendTimer();
    document.querySelectorAll('#code-row .code-digit').forEach(function(el) { el.value = ''; });
    var f = document.querySelector('#code-row .code-digit');
    if (f) f.focus();
  } catch(e) {
    showLoginError(2, 'Network error, please try again');
  }
}

function authBackToEmail() {
  document.getElementById('login-step-2').style.display = 'none';
  document.getElementById('login-step-1').style.display = '';
  showLoginError(2, '');
  if (resendTimer) { clearInterval(resendTimer); resendTimer = null; }
  document.querySelectorAll('#code-row .code-digit').forEach(function(el) { el.value = ''; });
}

async function authVerify() {
  var code = '';
  document.querySelectorAll('#code-row .code-digit').forEach(function(el) { code += el.value; });
  if (code.length !== 6 || !/^\d+$/.test(code)) {
    showLoginError(2, 'Please enter the 6-digit code'); return;
  }
  var btn = document.getElementById('login-verify-btn');
  btn.disabled = true; btn.textContent = 'Verifying...';
  showLoginError(2, '');
  try {
    var res = await fetch('/auth/verify', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: authEmail, code: code}),
    });
    if (!res.ok) {
      var err = await res.json();
      showLoginError(2, err.detail || 'Verification failed');
      btn.disabled = false; btn.textContent = 'Verify'; return;
    }
    document.getElementById('login-overlay').classList.add('hidden');
    document.getElementById('logout-btn').style.display = '';
    resetLoginForm();
    loadSessions();
  } catch(e) {
    showLoginError(2, 'Network error, please try again');
    btn.disabled = false; btn.textContent = 'Verify';
  }
}

function resetLoginForm() {
  authEmail = '';
  document.getElementById('login-email').value = '';
  document.querySelectorAll('#code-row .code-digit').forEach(function(el) { el.value = ''; });
  document.getElementById('login-step-2').style.display = 'none';
  document.getElementById('login-step-1').style.display = '';
  showLoginError(1, ''); showLoginError(2, '');
  if (resendTimer) { clearInterval(resendTimer); resendTimer = null; }
  resendSeconds = 0; updateResendLink();
}

async function authLogout() {
  await fetch('/auth/logout', { method: 'POST' });
  document.getElementById('login-overlay').classList.remove('hidden');
  document.getElementById('logout-btn').style.display = 'none';
  resetLoginForm();
  currentSessionId = null;
  document.getElementById('current-session-title').textContent = 'New Chat';
  msgs.innerHTML = '<div class="welcome" id="welcome"><div class="welcome-icon">&#x2699;&#xfe0f;</div><h3>MultiClaw Agent</h3><p>Ask me anything, run tools, or create plans.</p></div>';
  document.getElementById('session-list').innerHTML = '';
  sessions = [];
}
```

- [ ] **Step 5: Add code-digit keyboard handlers**

Add after the auth JS, still before `</script>`:

```javascript
// Code digit: auto-advance, backspace, paste, enter
document.getElementById('code-row').addEventListener('input', function(e) {
  var el = e.target;
  if (!el.classList.contains('code-digit')) return;
  var val = el.value.replace(/\D/g, '');
  el.value = val.slice(-1);
  if (val && parseInt(el.dataset.idx) < 5) {
    var next = document.querySelector('.code-digit[data-idx="' + (parseInt(el.dataset.idx) + 1) + '"]');
    if (next) next.focus();
  }
});
document.getElementById('code-row').addEventListener('keydown', function(e) {
  var el = e.target;
  if (!el.classList.contains('code-digit')) return;
  if (e.key === 'Backspace' && !el.value && parseInt(el.dataset.idx) > 0) {
    var prev = document.querySelector('.code-digit[data-idx="' + (parseInt(el.dataset.idx) - 1) + '"]');
    if (prev) prev.focus();
  }
  if (e.key === 'Enter') { e.preventDefault(); authVerify(); }
});
document.getElementById('code-row').addEventListener('paste', function(e) {
  e.preventDefault();
  var paste = (e.clipboardData || window.clipboardData).getData('text').replace(/\D/g, '').slice(0, 6);
  document.querySelectorAll('#code-row .code-digit').forEach(function(el, i) {
    el.value = paste[i] || '';
  });
  if (paste.length === 6) authVerify();
});
document.getElementById('login-email').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { e.preventDefault(); authSendCode(); }
});
```

- [ ] **Step 6: Update page load**

At the very bottom of the script, replace `loadSessions();` with:

```javascript
checkAuth();
```

Also update `createSession()` — add user_id. Actually, the API handles user-scoping now, so no change needed in `createSession()`. But update `switchSession()` and `deleteSession()` — they should still work as-is since the API now verifies ownership.

One more: in the `send()` function, add error handling for 401 responses:

In the `send()` function, after `if (!res.body) throw new Error('Response body is not readable');`, add:

```javascript
if (res.status === 401) {
  document.getElementById('login-overlay').classList.remove('hidden');
  document.getElementById('logout-btn').style.display = 'none';
  throw new Error('Session expired, please sign in again');
}
```

---

### Task 11: Add config to multiclaw.toml

**Files:**
- Modify: `multiclaw.toml`

- [ ] **Step 1: Append config**

Add at end:

```toml
[auth]
# jwt_secret is auto-generated and persisted in DB — leave empty
jwt_secret = ""

[brevo]
api_key = "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"
sender_email = ""
sender_name = "MultiClaw"
```

---

### Task 12: Run tests and verify

**Files:**
- No new files (smoke test via running the server)

- [ ] **Step 1: Run existing tests**

Run: `pytest tests/ -v --ignore=tests/test_frontend_debug.py --ignore=tests/test_frontend_welcome.py -x`
Expected: PASS (or pre-existing failures unrelated to auth)

- [ ] **Step 2: Start the server and verify**

Run: `python -m uvicorn multiclaw.server:app --host 127.0.0.1 --port 8000`

Then in another terminal:
```bash
# Test public access returns 401
curl -s http://localhost:8000/sessions | head -1
# Expected: {"detail":"Unauthorized"}

# Test send-code
curl -s -X POST http://localhost:8000/auth/send-code \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com"}'
# Expected: {"ok":true} (or 502 if Brevo config not set)

# Test me endpoint (unauthenticated)
curl -s http://localhost:8000/auth/me
# Expected: {"email":null,"user_id":null}

# Test index page loads
curl -s http://localhost:8000/ | head -1
# Expected: <!DOCTYPE html>
```
