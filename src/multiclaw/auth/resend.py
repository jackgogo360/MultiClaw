import logging

import httpx

from multiclaw.auth.email_content import (
    VERIFICATION_EMAIL_SUBJECT,
    build_verification_email_html,
)
from multiclaw.config import Settings

logger = logging.getLogger("multiclaw")
RESEND_API_URL = "https://api.resend.com/emails"


async def send_verification_code(settings: Settings, to_email: str, code: str) -> None:
    resend = settings.resend
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {resend.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"{resend.sender_name} <{resend.sender_email}>",
                "to": [to_email],
                "subject": VERIFICATION_EMAIL_SUBJECT,
                "html": build_verification_email_html(code),
            },
        )
        if resp.is_error:
            logger.error("Resend API error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
