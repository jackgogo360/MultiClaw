from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import SplitResult, urlsplit, urlunsplit


HTTP_SCHEMES = {"http", "https"}
EXPLICIT_NON_HTTP_SCHEMES = {
    "about",
    "data",
    "file",
    "ftp",
    "ftps",
    "gopher",
    "javascript",
    "mailto",
    "ssh",
}


class NetworkPolicyError(ValueError):
    pass


class NetworkPolicy:
    def __init__(self, allow_private_networks: bool = False) -> None:
        self.allow_private_networks = allow_private_networks

    def validate_url(self, url: str) -> str:
        normalized = self._normalize_url(url)
        try:
            parsed = urlsplit(normalized)
        except ValueError as exc:
            raise NetworkPolicyError("invalid URL") from exc

        scheme = parsed.scheme.lower()
        if scheme not in HTTP_SCHEMES:
            raise NetworkPolicyError(f"unsupported URL scheme: {scheme or 'missing'}")
        if parsed.username or parsed.password:
            raise NetworkPolicyError("URL credentials are not allowed")
        if not parsed.hostname:
            raise NetworkPolicyError("URL hostname is required")
        try:
            port = parsed.port
        except ValueError as exc:
            raise NetworkPolicyError("invalid URL") from exc

        self._validate_resolved_addresses(parsed.hostname, port or self._default_port(scheme))
        return urlunsplit(parsed)

    def _normalize_url(self, url: str) -> str:
        candidate = url.strip()
        if not candidate:
            return candidate
        if self._has_explicit_scheme(candidate):
            return candidate
        return f"https://{candidate}"

    def _has_explicit_scheme(self, url: str) -> bool:
        if "://" in url:
            return True
        match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", url)
        if not match:
            return False
        scheme = match.group(1).lower()
        remainder = url[match.end():]
        if scheme in HTTP_SCHEMES or scheme in EXPLICIT_NON_HTTP_SCHEMES:
            return True
        if re.match(r"^\d+([/?#].*)?$", remainder):
            return False
        return True

    def _default_port(self, scheme: str) -> int:
        return 80 if scheme == "http" else 443

    def _validate_resolved_addresses(self, hostname: str, port: int) -> None:
        try:
            infos = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, socket.gaierror) as exc:
            raise NetworkPolicyError(f"could not resolve hostname: {hostname}") from exc

        if not infos:
            raise NetworkPolicyError(f"could not resolve hostname: {hostname}")

        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            raw_ip = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                raise NetworkPolicyError(f"could not resolve hostname: {hostname}") from exc
            if family == socket.AF_INET6 and isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
                ip_obj = ip_obj.ipv4_mapped
            addresses.add(ip_obj)

        if not addresses:
            raise NetworkPolicyError(f"could not resolve hostname: {hostname}")

        if self.allow_private_networks:
            return

        if any(self._is_blocked_address(ip_obj) for ip_obj in addresses):
            raise NetworkPolicyError(f"blocked network target: {hostname}")

    def _is_blocked_address(
        self,
        ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return (
            not ip_obj.is_global
            or ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_unspecified
            or ip_obj.is_reserved
            or ip_obj.is_multicast
        )
