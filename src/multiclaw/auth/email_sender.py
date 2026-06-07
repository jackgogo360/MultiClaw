from multiclaw.auth.brevo import send_verification_code as send_brevo_verification_code
from multiclaw.auth.resend import send_verification_code as send_resend_verification_code
from multiclaw.config import Settings


def get_active_provider(settings: Settings) -> str:
    return settings.email.provider


def is_mock_enabled(settings: Settings) -> bool:
    provider = get_active_provider(settings)
    if provider == "resend":
        return settings.resend.mock
    return settings.brevo.mock


async def send_verification_code(settings: Settings, to_email: str, code: str) -> None:
    provider = get_active_provider(settings)
    if provider == "resend":
        await send_resend_verification_code(settings, to_email, code)
        return
    await send_brevo_verification_code(settings, to_email, code)
