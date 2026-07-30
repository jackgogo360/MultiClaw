import socket

import pytest

from multiclaw.tools.network_policy import NetworkPolicy, NetworkPolicyError


PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"


def install_fake_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    def fake_getaddrinfo(host: str, port: int | None, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"mock failure for {host}")
        results = []
        for ip in mapping[host]:
            if ":" in ip:
                results.append(
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (ip, port or 0, 0, 0),
                    )
                )
            else:
                results.append(
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        (ip, port or 0),
                    )
                )
        return results

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    ("url", "error_fragment"),
    [
        ("file:///etc/passwd", "unsupported URL scheme"),
        ("ftp://example.com/archive.zip", "unsupported URL scheme"),
        ("mailto:admin@example.com", "unsupported URL scheme"),
        ("custom:payload", "unsupported URL scheme"),
        ("https://user:pass@example.com/secret", "URL credentials are not allowed"),
        ("https://user@example.com/secret", "URL credentials are not allowed"),
        ("https:///missing-host", "URL hostname is required"),
        ("https://localhost", "blocked network target"),
        ("https://127.0.0.1", "blocked network target"),
        ("https://[::1]", "blocked network target"),
        ("https://[::ffff:127.0.0.1]", "blocked network target"),
        ("https://10.0.0.1", "blocked network target"),
        ("https://169.254.169.254/latest/meta-data", "blocked network target"),
        ("https://0.0.0.0", "blocked network target"),
        ("https://224.0.0.1", "blocked network target"),
        ("https://240.0.0.1", "blocked network target"),
        ("https://example.com:99999", "invalid URL"),
    ],
)
def test_validate_url_rejects_unsafe_targets(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    error_fragment: str,
) -> None:
    install_fake_resolver(
        monkeypatch,
        {
            "example.com": [PUBLIC_IPV4],
            "localhost": ["127.0.0.1"],
            "127.0.0.1": ["127.0.0.1"],
            "::1": ["::1"],
            "::ffff:127.0.0.1": ["::ffff:127.0.0.1"],
            "10.0.0.1": ["10.0.0.1"],
            "169.254.169.254": ["169.254.169.254"],
            "0.0.0.0": ["0.0.0.0"],
            "224.0.0.1": ["224.0.0.1"],
            "240.0.0.1": ["240.0.0.1"],
        },
    )

    with pytest.raises(NetworkPolicyError, match=error_fragment):
        NetworkPolicy().validate_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("example.com/path?q=1", "https://example.com/path?q=1"),
        ("example.com:8443/path", "https://example.com:8443/path"),
        ("https://public-v6.example/docs", "https://public-v6.example/docs"),
    ],
)
def test_validate_url_accepts_public_targets(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected: str,
) -> None:
    install_fake_resolver(
        monkeypatch,
        {
            "example.com": [PUBLIC_IPV4],
            "public-v6.example": [PUBLIC_IPV6],
        },
    )

    assert NetworkPolicy().validate_url(url) == expected


def test_validate_url_blocks_mixed_public_and_private_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_resolver(
        monkeypatch,
        {
            "mixed.example": [PUBLIC_IPV4, "10.0.0.7"],
        },
    )

    with pytest.raises(NetworkPolicyError) as exc_info:
        NetworkPolicy().validate_url("https://mixed.example/data")

    message = str(exc_info.value)
    assert "blocked network target" in message
    assert "mixed.example" in message
    assert "10.0.0.7" not in message


def test_allow_private_networks_permits_private_ip_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_resolver(
        monkeypatch,
        {
            "internal.example": ["10.1.2.3"],
        },
    )

    assert (
        NetworkPolicy(allow_private_networks=True).validate_url("internal.example/service")
        == "https://internal.example/service"
    )


def test_allow_private_networks_still_rejects_non_http_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_resolver(monkeypatch, {"internal.example": ["10.1.2.3"]})
    policy = NetworkPolicy(allow_private_networks=True)

    with pytest.raises(NetworkPolicyError, match="unsupported URL scheme"):
        policy.validate_url("file:///etc/passwd")

    with pytest.raises(NetworkPolicyError, match="URL credentials are not allowed"):
        policy.validate_url("https://user:pass@internal.example/service")


def test_validate_url_rejects_resolution_failures_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_resolver(monkeypatch, {})

    with pytest.raises(NetworkPolicyError) as exc_info:
        NetworkPolicy().validate_url("https://missing.example")

    message = str(exc_info.value)
    assert "could not resolve hostname" in message
    assert "missing.example" in message
