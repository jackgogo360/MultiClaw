from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, replace
from struct import pack
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .keyring import KEYRING_PROVIDER_NAME, DeploymentKeyring, SecretKeyringError


SECRET_ENVELOPE_FORMAT_VERSION = 1
SECRET_ENVELOPE_ALGORITHM = "AES-256-GCM"
SECRET_ENVELOPE_AAD_PREFIX = b"multiclaw.secret-envelope.v1\0"


class SecretEnvelopeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EnvelopeFields:
    tenant_id: str
    workspace_id: str | None
    secret_id: str
    provider_kind: str
    provider_name: str
    secret_name: str


@dataclass(frozen=True, slots=True)
class EncryptedSecretRecord:
    key_provider_name: str
    format_version: int
    algorithm: str
    key_version: int
    nonce: bytes
    ciphertext: bytes

    def replace(self, **changes) -> "EncryptedSecretRecord":
        return replace(self, **changes)


def _encode_aad_field(value: str | None) -> bytes:
    if value is None:
        return pack(">I", 0xFFFFFFFF)
    encoded = value.encode("utf-8")
    return pack(">I", len(encoded)) + encoded


def build_envelope_aad(
    fields: EnvelopeFields,
    *,
    key_provider_name: str = KEYRING_PROVIDER_NAME,
    key_version: int,
    format_version: int = SECRET_ENVELOPE_FORMAT_VERSION,
    algorithm: str = SECRET_ENVELOPE_ALGORITHM,
) -> bytes:
    pieces = [
        fields.tenant_id,
        fields.workspace_id,
        fields.secret_id,
        fields.provider_kind,
        fields.provider_name,
        fields.secret_name,
        key_provider_name,
        str(key_version),
        str(format_version),
        algorithm,
    ]
    encoded = bytearray(SECRET_ENVELOPE_AAD_PREFIX)
    for piece in pieces:
        encoded.extend(_encode_aad_field(piece))
    return bytes(encoded)


class SecretEnvelopeService:
    def __init__(
        self,
        keyring: DeploymentKeyring,
        *,
        nonce_source: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._keyring = keyring
        self._nonce_source = nonce_source

    def encrypt(
        self,
        plaintext: bytes,
        fields: EnvelopeFields,
        *,
        key_version: int | None = None,
    ) -> EncryptedSecretRecord:
        version = key_version or self._keyring.active_key_version
        key = self._keyring.get_key(version)
        nonce = self._nonce_source(12)
        if len(nonce) != 12:
            raise SecretEnvelopeError("nonce must be 12 bytes")
        aad = build_envelope_aad(
            fields,
            key_provider_name=KEYRING_PROVIDER_NAME,
            key_version=version,
        )
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        return EncryptedSecretRecord(
            key_provider_name=KEYRING_PROVIDER_NAME,
            format_version=SECRET_ENVELOPE_FORMAT_VERSION,
            algorithm=SECRET_ENVELOPE_ALGORITHM,
            key_version=version,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def decrypt(self, record: EncryptedSecretRecord, fields: EnvelopeFields) -> bytearray:
        self._validate_record(record)
        try:
            key = self._keyring.get_key(record.key_version)
        except SecretKeyringError as exc:
            raise SecretEnvelopeError("unknown key version") from exc

        aad = build_envelope_aad(
            fields,
            key_provider_name=record.key_provider_name,
            key_version=record.key_version,
            format_version=record.format_version,
            algorithm=record.algorithm,
        )
        try:
            plaintext = AESGCM(key).decrypt(record.nonce, record.ciphertext, aad)
        except InvalidTag as exc:
            raise SecretEnvelopeError("secret envelope integrity check failed") from exc
        return bytearray(plaintext)

    @staticmethod
    def _validate_record(record: EncryptedSecretRecord) -> None:
        if record.key_provider_name != KEYRING_PROVIDER_NAME:
            raise SecretEnvelopeError("invalid envelope provider")
        if record.format_version != SECRET_ENVELOPE_FORMAT_VERSION:
            raise SecretEnvelopeError("invalid envelope format")
        if record.algorithm != SECRET_ENVELOPE_ALGORITHM:
            raise SecretEnvelopeError("invalid envelope algorithm")
        if len(record.nonce) != 12:
            raise SecretEnvelopeError("invalid envelope nonce")
        if len(record.ciphertext) < 16:
            raise SecretEnvelopeError("invalid envelope ciphertext")
