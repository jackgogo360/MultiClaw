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
