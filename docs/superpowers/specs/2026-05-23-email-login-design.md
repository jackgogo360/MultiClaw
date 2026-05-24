# Email Login Design

## Overview

Add email-based login via Brevo verification codes. All existing endpoints become protected; unauthenticated users are blocked. Users can only see their own sessions.

- 6-digit code, 15-minute expiry
- Max 3 sends per email per day
- JWT token in httpOnly cookie, 10-day validity
- Login page embedded in existing single-page HTML

## Data Model

New tables in the existing SQLite database (`multiclaw.db`):

**`users`**

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| email | TEXT UNIQUE NOT NULL | |
| created_at | DATETIME | UTC |

**`verification_codes`**

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID |
| email | TEXT NOT NULL | |
| code | TEXT NOT NULL | 6-digit string |
| expires_at | DATETIME | now + 15 min |
| used | BOOL DEFAULT 0 | Set on successful verify |
| created_at | DATETIME | |

**Modifications to existing `sessions` table:**

Add column `user_id TEXT NOT NULL REFERENCES users(id)`.
Existing sessions are deleted on migration (dev preview, acceptable data loss).

## Configuration

**`multiclaw.toml` additions:**

```toml
[auth]
jwt_secret = "auto-generated-64-char-hex"

[brevo]
api_key = "xkeysib-..."
sender_email = "noreply@multiclaw.example.com"
sender_name = "MultiClaw"
```

**Pydantic settings additions (`Settings` class):**

- `auth`: `AuthSettings(jwt_secret: str)` — env override: `MULTICLAW_AUTH__JWT_SECRET`
- `brevo`: `BrevoSettings(api_key: str, sender_email: str, sender_name: str)` — env overrides: `MULTICLAW_BREVO__API_KEY`, etc.

A random `jwt_secret` is generated on first startup if not configured.

## API Endpoints

### Public (no auth required)

**`POST /auth/send-code`**

- Request: `{ "email": "user@example.com" }`
- Logic:
  1. Validate email format, reject invalid
  2. Count verification_codes for this email in last 24h → if >= 3, return `429 { "detail": "Too many attempts, try again tomorrow" }`
  3. Generate 6-digit random code, insert into verification_codes (expires_at = now + 15min)
  4. Call Brevo `POST /v3/smtp/email` with the code in HTML body
  5. Return `200 { "ok": true }`
- Errors: 422 (invalid email), 429 (rate limited), 502 (Brevo failure)

**`POST /auth/verify`**

- Request: `{ "email": "user@example.com", "code": "428917" }`
- Logic:
  1. Find latest unexpired, unused code matching email
  2. If none or mismatch → `401 { "detail": "Invalid or expired code" }`
  3. Mark code as `used`
  4. Find or create user by email
  5. Sign JWT: `{ sub: user.id, email: user.email, exp: now + 10 days }`
  6. Set cookie: `token=<jwt>; HttpOnly; SameSite=Lax; Max-Age=864000; Path=/`
  7. Return `200 { "ok": true, "email": user.email }`
- Errors: 401 (bad code), 422 (validation)

**`POST /auth/logout`**

- Clear the `token` cookie
- Return `200 { "ok": true }`

**`GET /auth/me`**

- If valid JWT in cookie → `200 { "email": "...", "user_id": "..." }`
- If none/invalid → return `200 { "email": null }` (never 401, used by frontend to detect auth state)

### Protected (auth required — existing endpoints)

All existing endpoints (`/chat`, `/sessions`, `/approve`, etc.) require valid JWT. On missing/invalid JWT: API routes return 401, HTML route returns redirect to login state.

Session endpoints are scoped to the authenticated user:

- `GET /sessions` — filter by `user_id`
- `POST /sessions` — auto-assign to current user
- `DELETE /sessions/{id}` — verify ownership before delete
- `GET /sessions/{id}/messages` — verify ownership

## Auth Middleware

A FastAPI middleware runs on every request:

1. Extract `token` from cookie
2. If absent → set `request.state.user = None`
3. If present → verify JWT signature + expiry with `jwt_secret`
4. Valid → set `request.state.user = {"id": ..., "email": ...}`
5. Invalid/expired → set `request.state.user = None`

A `require_auth` dependency (used in endpoint signatures) checks `request.state.user` and raises `HTTPException(401)` if None.

For the HTML index route: if unauthenticated, still serve the page (the frontend handles showing login vs chat).

## Brevo Integration

New module: `src/multiclaw/auth/brevo.py`

Uses Brevo transactional email API: `POST https://api.brevo.com/v3/smtp/email`

```python
def send_verification_code(settings, to_email: str, code: str) -> None:
    payload = {
        "sender": {"name": settings.sender_name, "email": settings.sender_email},
        "to": [{"email": to_email}],
        "subject": "MultiClaw Verification Code",
        "htmlContent": f"<p>Your verification code is: <strong>{code}</strong></p><p>Expires in 15 minutes.</p>"
    }
    # POST to Brevo with api-key header
```

## Frontend Changes

All changes in `src/multiclaw/static/index.html`:

### Login Screen

Two-step flow, displayed as a centered card when `GET /auth/me` returns no user:

1. **Step 1 — Email:** input + "Send Code" button. On submit:
   - POST `/auth/send-code`
   - Button shows loading spinner
   - On success: transition to step 2, start 60s resend cooldown

2. **Step 2 — Code:** 6 individual digit inputs (auto-advance on input, supports paste of full code) + "Verify" button. On submit:
   - POST `/auth/verify`
   - On success: transition to chat interface, reload sessions
   - On error: show error message below inputs

### States

- **Loading:** button disabled + spinner text ("Sending...")
- **Error:** red text below the card ("Invalid code", "Too many attempts" etc.)
- **Resend cooldown:** after sending, "Resend code (59s)" with countdown
- **Expired session:** if JWT expires mid-use, API 401 → show login screen
- **Back link:** from step 2 back to step 1 ("← Use a different email")

### Auth Check on Load

1. Page loads → `GET /auth/me`
2. Has user → render chat interface (current behavior)
3. No user → render login screen, hide chat

## Error Handling

| Scenario | HTTP Code | User-facing message |
|----------|-----------|---------------------|
| Invalid email format | 422 | "Please enter a valid email" |
| Too many sends (24h) | 429 | "Too many attempts, please try again tomorrow" |
| Brevo API failure | 502 | "Failed to send email, please try again later" |
| Wrong/expired code | 401 | "Invalid or expired verification code" |
| Missing JWT on protected route | 401 | N/A (API), redirect to login (HTML) |
| Expired JWT | 401 | Same as above |

## Security Notes

- JWT signed with HS256 using `jwt_secret` from config
- httpOnly cookie prevents JS access to token
- SameSite=Lax prevents most CSRF while allowing normal navigation
- No refresh mechanism — expires after 10 days, user re-authenticates
- Verification codes stored in DB (not in memory) so they survive server restarts
- Rate limiting on send-code prevents abuse

## Files to Create/Modify

| File | Action |
|------|--------|
| `src/multiclaw/auth/__init__.py` | Create |
| `src/multiclaw/auth/brevo.py` | Create |
| `src/multiclaw/auth/middleware.py` | Create |
| `src/multiclaw/auth/models.py` | Create |
| `src/multiclaw/auth/router.py` | Create |
| `src/multiclaw/config/settings.py` | Modify — add AuthSettings, BrevoSettings |
| `src/multiclaw/server.py` | Modify — add auth middleware, register router |
| `src/multiclaw/session/sqlite.py` | Modify — add user_id to schema |
| `src/multiclaw/static/index.html` | Modify — add login screen |
| `multiclaw.toml` | Modify — add [auth] and [brevo] sections |
