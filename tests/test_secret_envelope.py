import base64
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from multiclaw.config.settings import SecretSettings
from multiclaw.secrets.envelope import (
    EnvelopeFields,
    EncryptedSecretRecord,
    SecretEnvelopeError,
    SecretEnvelopeService,
    build_envelope_aad,
)
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError


_KEY_V1 = base64.b64encode(bytes([7]) * 32).decode("ascii")
_KEY_V3 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


def _keyring_payload() -> str:
    return base64.b64encode(
        json.dumps(
            {
                "active_key_version": 3,
                "keys": {
                    "1": _KEY_V1,
                    "3": _KEY_V3,
                },
            }
        ).encode("utf-8")
    ).decode("ascii")


def _fields(secret_id: str = "secret-a") -> EnvelopeFields:
    return EnvelopeFields(
        tenant_id="tenant-a",
        workspace_id=None,
        secret_id=secret_id,
        provider_kind="llm",
        provider_name="openai",
        secret_name="api_key",
    )


def _record(**kwargs) -> EncryptedSecretRecord:
    return EncryptedSecretRecord(
        key_provider_name="deployment-keyring",
        format_version=1,
        algorithm="AES-256-GCM",
        key_version=3,
        nonce=bytes.fromhex("000102030405060708090a0b"),
        ciphertext=bytes.fromhex(
            "3367a56fe896a778ff24e3a6c7881418e6fefe9b30454bf5716530532311188e4f"
        ),
        **kwargs,
    )


def test_keyring_requires_exactly_one_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyring_file = tmp_path / "keyring.json"
    keyring_file.write_text(base64.b64decode(_keyring_payload()).decode("utf-8"), encoding="utf-8")
    keyring_file.chmod(0o600)

    with pytest.raises(SecretKeyringError, match="exactly one"):
        DeploymentKeyring.load(SecretSettings(), environ={})

    monkeypatch.setenv("MULTICLAW_SECRETS_KEYRING_B64", _keyring_payload())
    with pytest.raises(SecretKeyringError, match="exactly one"):
        DeploymentKeyring.load(
            SecretSettings(keyring_file=str(keyring_file)),
            environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
        )


@pytest.mark.parametrize(
    "payload",
    [
        "not-base64",
        base64.b64encode(b"not-json").decode("ascii"),
        base64.b64encode(json.dumps({"active_key_version": 3}).encode("utf-8")).decode("ascii"),
        base64.b64encode(
            json.dumps(
                {
                    "active_key_version": 3,
                    "keys": {"3": base64.b64encode(b"short").decode("ascii")},
                }
            ).encode("utf-8")
        ).decode("ascii"),
        base64.b64encode(
            json.dumps(
                {
                    "active_key_version": 2,
                    "keys": {"3": _KEY_V3},
                }
            ).encode("utf-8")
        ).decode("ascii"),
    ],
)
def test_keyring_rejects_malformed_payloads(payload: str) -> None:
    with pytest.raises(SecretKeyringError):
        DeploymentKeyring.load(SecretSettings(), environ={"MULTICLAW_SECRETS_KEYRING_B64": payload})


def test_keyring_rejects_group_or_world_readable_file(tmp_path: Path) -> None:
    keyring_file = tmp_path / "keyring.json"
    keyring_file.write_text(base64.b64decode(_keyring_payload()).decode("utf-8"), encoding="utf-8")
    keyring_file.chmod(0o644)

    with pytest.raises(SecretKeyringError, match="permissions"):
        DeploymentKeyring.load(SecretSettings(keyring_file=str(keyring_file)), environ={})


def test_keyring_rejects_boolean_versions_in_json() -> None:
    payload = base64.b64encode(
        json.dumps(
            {
                "active_key_version": True,
                "keys": {"1": _KEY_V1},
            }
        ).encode("utf-8")
    ).decode("ascii")

    with pytest.raises(SecretKeyringError, match="active key version"):
        DeploymentKeyring.load(SecretSettings(), environ={"MULTICLAW_SECRETS_KEYRING_B64": payload})


def test_keyring_rejects_duplicate_normalized_versions() -> None:
    payload = base64.b64encode(
        json.dumps(
            {
                "active_key_version": 1,
                "keys": {"1": _KEY_V1, "01": _KEY_V3},
            }
        ).encode("utf-8")
    ).decode("ascii")

    with pytest.raises(SecretKeyringError, match="duplicate normalized"):
        DeploymentKeyring.load(SecretSettings(), environ={"MULTICLAW_SECRETS_KEYRING_B64": payload})


def test_keyring_rejects_symlink_file_source(tmp_path: Path) -> None:
    target = tmp_path / "real-keyring.json"
    target.write_text(base64.b64decode(_keyring_payload()).decode("utf-8"), encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "keyring-link.json"
    symlink.symlink_to(target)

    with pytest.raises(SecretKeyringError, match="unavailable|cannot be read|symlink"):
        DeploymentKeyring.load(SecretSettings(keyring_file=str(symlink)), environ={})


def test_keyring_file_loader_uses_single_fd_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    keyring_file = tmp_path / "keyring.json"
    keyring_file.write_text(base64.b64decode(_keyring_payload()).decode("utf-8"), encoding="utf-8")
    keyring_file.chmod(0o600)
    events: list[tuple[str, object]] = []
    real_open = os.open
    real_fstat = os.fstat
    real_fdopen = os.fdopen

    def tracking_open(path, flags, mode=0o777):
        events.append(("open", os.fspath(path)))
        return real_open(path, flags, mode)

    def tracking_fstat(fd):
        events.append(("fstat", fd))
        return real_fstat(fd)

    def tracking_fdopen(fd, mode="r", encoding=None, closefd=True):
        events.append(("fdopen", fd))
        return real_fdopen(fd, mode, encoding=encoding, closefd=closefd)

    def forbidden_read_text(*args, **kwargs):
        raise AssertionError("Path.read_text should not be used")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fstat", tracking_fstat)
    monkeypatch.setattr(os, "fdopen", tracking_fdopen)
    monkeypatch.setattr(Path, "read_text", forbidden_read_text)

    keyring = DeploymentKeyring.load(SecretSettings(keyring_file=str(keyring_file)), environ={})

    assert keyring.active_key_version == 3
    assert [event[0] for event in events] == ["open", "fstat", "fdopen"]


def test_keyring_mapping_is_immutable_and_referenced_versions_fail_closed() -> None:
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )

    with pytest.raises(TypeError):
        keyring.keys[4] = b"x"  # type: ignore[index]

    keyring.require_versions({1: 2, 3: 1})

    with pytest.raises(SecretKeyringError, match="missing referenced key versions"):
        keyring.require_versions({1: 1, 9: 1})


def test_fixed_vector_encrypt_and_decrypt_matches_contract() -> None:
    vector = json.loads(
        (Path(__file__).parent / "vectors" / "secret_envelope_v1.json").read_text(encoding="utf-8")
    )
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    service = SecretEnvelopeService(
        keyring,
        nonce_source=lambda length: bytes.fromhex(vector["nonce_hex"]) if length == 12 else b"",
    )

    fields = _fields()
    aad = build_envelope_aad(fields, key_provider_name="deployment-keyring", key_version=3)
    record = service.encrypt(bytes.fromhex(vector["plaintext_hex"]), fields)

    assert aad == bytes.fromhex(vector["aad_hex"])
    assert record.ciphertext == bytes.fromhex(vector["ciphertext_with_tag_hex"])
    assert bytes(service.decrypt(record, fields)) == bytes.fromhex(vector["plaintext_hex"])


def test_decrypt_rejects_row_swap_and_tampering() -> None:
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    service = SecretEnvelopeService(keyring, nonce_source=lambda _length: b"\x09" * 12)
    fields = _fields()
    record = service.encrypt(b"secret-value", fields)

    swapped = replace(fields, secret_id="secret-b")
    tampered = replace(record, ciphertext=record.ciphertext[:-1] + b"\x00")

    with pytest.raises(SecretEnvelopeError, match="integrity"):
        service.decrypt(record, swapped)

    with pytest.raises(SecretEnvelopeError, match="integrity"):
        service.decrypt(tampered, fields)


@pytest.mark.parametrize(
    ("mutated", "pattern"),
    [
        (replace(_record(), key_provider_name="other"), "provider"),
        (replace(_record(), format_version=2), "format"),
        (replace(_record(), algorithm="AES-128-GCM"), "algorithm"),
        (replace(_record(), key_version=99), "key version"),
        (replace(_record(), nonce=b"short"), "nonce"),
        (replace(_record(), ciphertext=b"tiny"), "ciphertext"),
    ],
)
def test_decrypt_validates_fixed_record_shape(mutated: EncryptedSecretRecord, pattern: str) -> None:
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    service = SecretEnvelopeService(keyring)

    with pytest.raises(SecretEnvelopeError, match=pattern):
        service.decrypt(mutated, _fields())
