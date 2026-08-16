from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from multiclaw.config.settings import WorkflowSettings
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.auth import (
    AuthUserRepository,
    TenantUserRepository,
    VerificationCodeRepository,
    WorkspaceRepository,
)
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.secrets import SecretsRepository
from multiclaw.storage.repositories.sessions import SessionRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.tenancy.context import TenantContext


SelfType = TypeVar("SelfType", bound="_BaseUnitOfWork")


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


class _BaseUnitOfWork(Generic[SelfType]):
    def __init__(self, database: Database, *, read_only: bool = False) -> None:
        self._database = database
        self._read_only = read_only
        self.conn: AsyncConnection | None = None
        self._tx: AsyncTransaction | None = None
        self._after_tx_cleanup: list[tuple[str, Callable[[], Awaitable[None]]]] = []

    async def __aenter__(self: SelfType) -> SelfType:
        try:
            self.conn = await self._database.engine.connect()
            if not self._read_only:
                self._tx = await self._database.dialect.begin_write(self.conn)
            self._bind_repositories()
            return self
        except BaseException as primary:
            await self._cleanup_after_failure(
                primary=primary,
                rollback_phase="rollback" if self._tx is not None else None,
                close_phase="close",
            )
            self.conn = None
            self._tx = None
            raise primary

    async def __aexit__(self, exc_type, exc, tb) -> None:
        primary: BaseException | None = exc
        try:
            if self._tx is not None and self._tx.is_active:
                if primary is None:
                    try:
                        await self._tx.commit()
                    except BaseException as error:
                        primary = error
                else:
                    await self._cleanup_after_failure(
                        primary=primary,
                        rollback_phase="rollback",
                        close_phase=None,
                    )
        finally:
            if primary is None:
                await self._close_without_primary()
            else:
                await self._cleanup_after_failure(
                    primary=primary,
                    rollback_phase=None,
                    close_phase="close",
                )
            self.conn = None
            self._tx = None
        if primary is not None:
            raise primary

    def _bind_repositories(self) -> None:
        raise NotImplementedError

    async def commit(self) -> None:
        if self._tx is None or not self._tx.is_active:
            return
        await self._tx.commit()

    def _register_after_tx_cleanup(
        self,
        phase: str,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._after_tx_cleanup.append((phase, callback))

    async def _cleanup_after_failure(
        self,
        *,
        primary: BaseException,
        rollback_phase: str | None,
        close_phase: str | None,
    ) -> None:
        if rollback_phase is not None and self._tx is not None and self._tx.is_active:
            try:
                await self._tx.rollback()
            except BaseException as error:
                _note_cleanup_error(primary, rollback_phase, error)
        await self._run_after_tx_cleanup(primary)
        if close_phase is not None and self.conn is not None:
            try:
                await self.conn.close()
            except BaseException as error:
                _note_cleanup_error(primary, close_phase, error)

    async def _close_without_primary(self) -> None:
        primary: BaseException | None = None
        try:
            await self._run_after_tx_cleanup(None)
        except BaseException as error:
            primary = error
        if self.conn is not None:
            if (
                self._read_only
                and hasattr(self.conn, "in_transaction")
                and self.conn.in_transaction()
            ):
                await self.conn.rollback()
            try:
                await self.conn.close()
            except BaseException as error:
                if primary is None:
                    raise
                _note_cleanup_error(primary, "close", error)
        if primary is not None:
            raise primary

    async def _run_after_tx_cleanup(self, primary: BaseException | None) -> None:
        callbacks = self._after_tx_cleanup
        self._after_tx_cleanup = []
        release_error: BaseException | None = primary
        for phase, callback in callbacks:
            try:
                await callback()
            except BaseException as error:
                if release_error is None:
                    release_error = error
                else:
                    _note_cleanup_error(release_error, phase, error)
        if primary is None and release_error is not None:
            raise release_error


class AuthUnitOfWork(_BaseUnitOfWork["AuthUnitOfWork"]):
    users: AuthUserRepository
    verification_codes: VerificationCodeRepository

    def __init__(self, database: Database, *, read_only: bool = False) -> None:
        super().__init__(database, read_only=read_only)

    def _bind_repositories(self) -> None:
        assert self.conn is not None
        self.users = AuthUserRepository(self.conn, self._database.dialect)
        self.verification_codes = VerificationCodeRepository(self.conn, self._database.dialect)
        self.verification_codes.attach_cleanup_registrar(self._register_after_tx_cleanup)


class TenantUnitOfWork(_BaseUnitOfWork["TenantUnitOfWork"]):
    users: TenantUserRepository
    workspaces: WorkspaceRepository
    sessions: SessionRepository
    memory: MemoryRepository
    secrets: SecretsRepository
    workflow: WorkflowRepository

    def __init__(
        self,
        database: Database,
        context: TenantContext,
        *,
        workflow_settings: WorkflowSettings | None = None,
    ) -> None:
        super().__init__(database)
        self._context = context
        self._workflow_settings = workflow_settings or WorkflowSettings()

    def _bind_repositories(self) -> None:
        assert self.conn is not None
        self.users = TenantUserRepository(self.conn, self._database.dialect, self._context)
        self.workspaces = WorkspaceRepository(self.conn, self._database.dialect, self._context)
        self.sessions = SessionRepository(self.conn, self._context, self._database.dialect)
        self.memory = MemoryRepository(self.conn, self._context, self._database.dialect)
        self.secrets = SecretsRepository(self.conn, self._database.dialect, self._context)
        self.workflow = WorkflowRepository(
            self.conn,
            self._database.dialect,
            self._workflow_settings.heartbeat_ms,
            self._workflow_settings.lease_ttl_ms,
        )
