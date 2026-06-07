import logging

import httpx

from multiclaw.auth.email_content import (
    VERIFICATION_EMAIL_SUBJECT,
    build_verification_email_html,
)
from multiclaw.config import Settings

logger = logging.getLogger("multiclaw")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


async def send_verification_code(settings: Settings, to_email: str, code: str) -> None:
    brevo = settings.brevo
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            BREVO_API_URL,
            headers={
                "accept": "application/json",
                "api-key": brevo.api_key,
                "content-type": "application/json",
            },
            json={
                "sender": {"name": brevo.sender_name, "email": brevo.sender_email},
                "to": [{"email": to_email}],
                "subject": VERIFICATION_EMAIL_SUBJECT,
                "htmlContent": build_verification_email_html(code),
            },
        )
        if resp.is_error:
            logger.error("Brevo API error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
