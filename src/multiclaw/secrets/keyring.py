from __future__ import annotations

import base64
import json
import os
import stat
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


KEYRING_PROVIDER_NAME = "deployment-keyring"


class SecretKeyringError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeploymentKeyring:
    active_key_version: int
    keys: Mapping[int, bytes]

    @classmethod
    def load(
        cls,
        settings,
        *,
        environ: Mapping[str, str] | None = None,
        provider_name: str = KEYRING_PROVIDER_NAME,
    ) -> "DeploymentKeyring":
        if provider_name != KEYRING_PROVIDER_NAME:
            raise SecretKeyringError("unknown secrets key provider")

        source_env = (environ or os.environ).get("MULTICLAW_SECRETS_KEYRING_B64", "").strip()
        source_file = str(getattr(settings, "keyring_file", "") or "").strip()
        if bool(source_env) == bool(source_file):
            raise SecretKeyringError("secrets keyring requires exactly one configured source")

        raw_json = (
            cls._load_from_base64(source_env)
            if source_env
            else cls._load_from_file(Path(source_file))
        )
        payload = cls._parse_payload(raw_json)
        return cls(
            active_key_version=payload["active_key_version"],
            keys=MappingProxyType(payload["keys"]),
        )

    @staticmethod
    def _load_from_base64(value: str) -> str:
        try:
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        except Exception as exc:
            raise SecretKeyringError("invalid secrets keyring encoding") from exc
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretKeyringError("invalid secrets keyring payload") from exc

    @staticmethod
    def _load_from_file(path: Path) -> str:
        fd: int | None = None
        try:
            if not hasattr(os, "O_NOFOLLOW"):
                raise SecretKeyringError("secrets keyring file cannot be read safely")
            fd = os.open(
                os.fspath(path),
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
            )
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SecretKeyringError("secrets keyring file is unavailable")
            mode = stat.S_IMODE(file_stat.st_mode)
            if mode & 0o077:
                raise SecretKeyringError("secrets keyring file permissions are too broad")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as handle:
                fd = None
                return handle.read(65536)
        except OSError as exc:
            raise SecretKeyringError("secrets keyring file cannot be read") from exc
        finally:
            if fd is not None:
                os.close(fd)

    @classmethod
    def _parse_payload(cls, raw_json: str) -> dict[str, object]:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise SecretKeyringError("invalid secrets keyring payload") from exc
        if not isinstance(payload, dict):
            raise SecretKeyringError("invalid secrets keyring payload")
        if set(payload) != {"active_key_version", "keys"}:
            raise SecretKeyringError("invalid secrets keyring contract")

        active = payload.get("active_key_version")
        keys = payload.get("keys")
        if type(active) is not int or active <= 0:
            raise SecretKeyringError("invalid active key version")
        if not isinstance(keys, dict) or not keys:
            raise SecretKeyringError("invalid keyring keys")

        parsed: dict[int, bytes] = {}
        for version_raw, key_raw in keys.items():
            if not isinstance(version_raw, str) or not version_raw.isdecimal():
                raise SecretKeyringError("invalid key version")
            version = int(version_raw, 10)
            if version <= 0:
                raise SecretKeyringError("invalid key version")
            if version in parsed:
                raise SecretKeyringError("duplicate normalized key versions are not allowed")
            if not isinstance(key_raw, str):
                raise SecretKeyringError("invalid key material")
            try:
                decoded = base64.b64decode(key_raw.encode("ascii"), validate=True)
            except Exception as exc:
                raise SecretKeyringError("invalid key material") from exc
            if len(decoded) != 32:
                raise SecretKeyringError("keyring keys must be 32 bytes")
            parsed[version] = decoded

        if active not in parsed:
            raise SecretKeyringError("active key version is missing")
        return {"active_key_version": active, "keys": parsed}

    def get_key(self, version: int) -> bytes:
        try:
            return self.keys[version]
        except KeyError as exc:
            raise SecretKeyringError("unknown key version") from exc

    def require_versions(self, version_counts: Mapping[int, int]) -> None:
        missing = sorted(
            version
            for version, count in version_counts.items()
            if count > 0 and version not in self.keys
        )
        if missing:
            raise SecretKeyringError("missing referenced key versions")
