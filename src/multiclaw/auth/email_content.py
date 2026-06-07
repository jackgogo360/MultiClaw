VERIFICATION_EMAIL_SUBJECT = "MultiClaw Verification Code"


def build_verification_email_html(code: str) -> str:
    return (
        '<div style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:24px">'
        '<h2 style="color:#333">Verification Code</h2>'
        '<p style="font-size:16px;color:#555">Your code is:</p>'
        '<div style="font-size:32px;font-weight:bold;letter-spacing:6px;'
        'padding:16px 24px;background:#f5f5f5;border-radius:8px;text-align:center;margin:16px 0">'
        f"{code}"
        "</div>"
        '<p style="font-size:13px;color:#999">Expires in 15 minutes.</p>'
        "</div>"
    )
