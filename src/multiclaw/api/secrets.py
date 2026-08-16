from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from multiclaw.api.dependencies import tenant_context, tenant_uow
from multiclaw.auth.middleware import require_recent_auth
from multiclaw.secrets.envelope import EnvelopeFields, SecretEnvelopeService
from multiclaw.secrets.resolver import SecretNotConfiguredError
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy import TenantContext


router = APIRouter(prefix="/api")


class SecretPutRequest(BaseModel):
    value: str


def _parse_provider(provider: str) -> tuple[str, str]:
    if ":" not in provider:
        return "llm", provider
    kind, name = provider.split(":", 1)
    if not kind or not name:
        raise HTTPException(status_code=422, detail="invalid secret provider")
    return kind, name


@router.get("/secrets")
async def list_secrets(
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    entries = await uow.secrets.list_metadata()
    return [
        {
            "providerKind": entry.provider_kind,
            "providerName": entry.provider_name,
            "secretName": entry.secret_name,
            "maskedValue": entry.masked_value,
            "updatedAt": entry.updated_at,
        }
        for entry in entries
    ]


@router.put("/secrets/{provider}/{name}")
async def put_secret(
    provider: str,
    name: str,
    body: SecretPutRequest,
    request: Request,
    _recent_user=Depends(require_recent_auth),
    context: TenantContext = Depends(tenant_context),
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    del _recent_user
    provider_kind, provider_name = _parse_provider(provider)
    keyring = getattr(request.app.state, "secret_keyring", None)
    if keyring is None:
        raise HTTPException(status_code=503, detail="secret storage unavailable")
    envelope = SecretEnvelopeService(keyring)
    secret_id = str(uuid4())
    metadata = await uow.secrets.put_encrypted(
        secret_id=secret_id,
        provider_kind=provider_kind,
        provider_name=provider_name,
        secret_name=name,
        record=envelope.encrypt(
            body.value.encode("utf-8"),
            EnvelopeFields(
                tenant_id=context.tenant_id,
                workspace_id=None,
                secret_id=secret_id,
                provider_kind=provider_kind,
                provider_name=provider_name,
                secret_name=name,
            ),
        ),
    )
    return {
        "providerKind": metadata.provider_kind,
        "providerName": metadata.provider_name,
        "secretName": metadata.secret_name,
        "maskedValue": metadata.masked_value,
        "updatedAt": metadata.updated_at,
    }


@router.delete("/secrets/{provider}/{name}")
async def delete_secret(
    provider: str,
    name: str,
    _recent_user=Depends(require_recent_auth),
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    del _recent_user
    provider_kind, provider_name = _parse_provider(provider)
    deleted = await uow.secrets.delete(provider_kind, provider_name, name)
    if not deleted:
        raise HTTPException(status_code=404, detail="secret not found")
    return {"ok": True}


@router.post("/secrets/{provider}/{name}/test")
async def test_secret(
    provider: str,
    name: str,
    request: Request,
    _recent_user=Depends(require_recent_auth),
    context: TenantContext = Depends(tenant_context),
):
    del _recent_user
    provider_kind, provider_name = _parse_provider(provider)
    resolver = getattr(request.app.state, "secret_resolver", None)
    if resolver is None:
        raise HTTPException(status_code=503, detail="secret storage unavailable")
    try:
        resolved = await resolver.resolve(context, provider_kind, provider_name, name)
    except SecretNotConfiguredError:
        raise HTTPException(status_code=404, detail="secret not found") from None
    resolved.close()
    return {"ok": True}
