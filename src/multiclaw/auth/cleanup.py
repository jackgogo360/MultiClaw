from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError, ProgrammingError

from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork

logger = logging.getLogger("multiclaw")


class AuthCleanupWorker:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def run_once(self) -> int:
        try:
            async with AuthUnitOfWork(self._database) as uow:
                return await uow.verification_codes.delete_expired_codes()
        except (OperationalError, ProgrammingError) as exc:
            if _is_missing_verification_code_table(exc):
                logger.info("auth cleanup disabled until verification_codes schema exists")
                return 0
            raise


def _is_missing_verification_code_table(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "verification_codes" in text and ("no such table" in text or "doesn't exist" in text)
