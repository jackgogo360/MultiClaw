from unittest.mock import Mock

import pytest

from multiclaw.config.settings import Settings


def test_settings_loads_email_provider_and_resend_section(tmp_path):
    config_path = tmp_path / "multiclaw.toml"
    config_path.write_text(
        "\n".join(
            [
                "[email]",
                'provider = "resend"',
                "",
                "[resend]",
                'api_key = "re_test_123"',
                'sender_email = "noreply@example.com"',
                'sender_name = "MultiClaw Auth"',
                "mock = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_config_file=str(config_path))

    assert settings.email.provider == "resend"
    assert settings.resend.api_key == "re_test_123"
    assert settings.resend.sender_email == "noreply@example.com"
    assert settings.resend.sender_name == "MultiClaw Auth"
    assert settings.resend.mock is True


@pytest.mark.asyncio
async def test_email_sender_dispatches_to_selected_provider(monkeypatch):
    from multiclaw.auth import email_sender

    calls: list[tuple[str, str, str]] = []

    async def fake_brevo(settings: Settings, to_email: str, code: str) -> None:
        calls.append(("brevo", to_email, code))

    async def fake_resend(settings: Settings, to_email: str, code: str) -> None:
        calls.append(("resend", to_email, code))

    monkeypatch.setattr(email_sender, "send_brevo_verification_code", fake_brevo)
    monkeypatch.setattr(email_sender, "send_resend_verification_code", fake_resend)

    settings = Settings(
        email={"provider": "resend"},
        brevo={"mock": False},
        resend={"mock": False},
    )

    await email_sender.send_verification_code(
        settings,
        "user@example.com",
        "123456",
    )

    assert calls == [("resend", "user@example.com", "123456")]


def test_email_sender_mock_flag_follows_selected_provider():
    from multiclaw.auth import email_sender

    settings = Settings(
        email={"provider": "resend"},
        brevo={"mock": False},
        resend={"mock": True},
    )

    assert email_sender.is_mock_enabled(settings) is True


@pytest.mark.asyncio
async def test_resend_sender_posts_expected_payload(monkeypatch):
    from multiclaw.auth import resend

    response = Mock()
    response.is_error = False
    response.raise_for_status = Mock()
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: int):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return response

    monkeypatch.setattr(resend.httpx, "AsyncClient", FakeAsyncClient)

    settings = Settings(
        resend={
            "api_key": "re_test_123",
            "sender_email": "noreply@example.com",
            "sender_name": "MultiClaw",
        }
    )

    await resend.send_verification_code(settings, "user@example.com", "123456")

    assert captured == {
        "timeout": 15,
        "url": resend.RESEND_API_URL,
        "headers": {
            "Authorization": "Bearer re_test_123",
            "Content-Type": "application/json",
        },
        "json": {
            "from": "MultiClaw <noreply@example.com>",
            "to": ["user@example.com"],
            "subject": "MultiClaw Verification Code",
            "html": (
                '<div style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:24px">'
                '<h2 style="color:#333">Verification Code</h2>'
                '<p style="font-size:16px;color:#555">Your code is:</p>'
                '<div style="font-size:32px;font-weight:bold;letter-spacing:6px;'
                'padding:16px 24px;background:#f5f5f5;border-radius:8px;text-align:center;margin:16px 0">'
                "123456"
                "</div>"
                '<p style="font-size:13px;color:#999">Expires in 15 minutes.</p>'
                "</div>"
            ),
        },
    }
    response.raise_for_status.assert_called_once_with()
