from typing import TYPE_CHECKING, Literal

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction
from sqlalchemy.sql.elements import ColumnElement

if TYPE_CHECKING:
    from multiclaw.tenancy.context import TenantContext


class SQLiteDialect:
    name: Literal["sqlite"] = "sqlite"

    def db_now_ms(self) -> ColumnElement[int]:
        return cast(func.floor((func.julianday("now") - 2440587.5) * 86400000), BigInteger)

    async def begin_write(self, connection: AsyncConnection) -> AsyncTransaction:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        transaction = connection.get_transaction()
        assert transaction is not None
        return transaction

    async def lock_run(self, connection: AsyncConnection, context: "TenantContext") -> None:
        return None


class MySQLDialect:
    name: Literal["mysql"] = "mysql"

    def db_now_ms(self) -> ColumnElement[int]:
        return cast(func.floor(func.unix_timestamp(func.current_timestamp(6)) * 1000), BigInteger)

    async def begin_write(self, connection: AsyncConnection) -> AsyncTransaction:
        return await connection.begin()

    async def lock_run(self, connection: AsyncConnection, context: "TenantContext") -> None:
        from multiclaw.storage.schema import agent_runs

        await connection.execute(
            select(agent_runs.c.run_id)
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
            )
            .with_for_update()
        )
