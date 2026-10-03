from uuid import uuid4

import pytest

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext


def test_collaboration_configuration_loads_from_toml(tmp_path):
    from multiclaw.config import Settings
    config = tmp_path / "app.toml"
    config.write_text("[collaboration]\nenabled=true\nmax_workers=4\n", encoding="utf-8")
    settings = Settings(_config_file=str(config))
    assert settings.collaboration.enabled and settings.collaboration.max_workers == 4


@pytest.fixture
async def scope(tmp_path):
    database = Database.create(DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'collaboration.db'}"))
    async with database.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(f"{uuid4()}@example.com")
    context = TenantContext(user.id, user.default_workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create()
    yield database, context.for_session(session.id)
    await database.dispose()


async def test_jobs_are_persisted_and_hidden_from_other_scope(scope):
    from multiclaw.collaboration.repository import CollaborationRepository

    database, context = scope
    async with database.write_transaction() as connection:
        repository = CollaborationRepository(connection, context, database.dialect)
        job = await repository.create_job({"goal": "inspect", "profile": "reader"})
    async with database.connect() as connection:
        repository = CollaborationRepository(connection, context, database.dialect)
        assert (await repository.get_job(job["job_id"]))["status"] == "queued"
        foreign = CollaborationRepository(connection, TenantContext(str(uuid4()), context.workspace_id, context.session_id), database.dialect)
        assert await foreign.get_job(job["job_id"]) is None


async def test_team_task_claim_is_cas_and_dependencies_are_enforced(scope):
    from multiclaw.collaboration.repository import CollaborationConflict, CollaborationRepository

    database, context = scope
    async with database.write_transaction() as connection:
        repository = CollaborationRepository(connection, context, database.dialect)
        team = await repository.create_team("investigate", [
            {"name": "lead", "role": "leader", "profile": "reader"},
            {"name": "research", "role": "member", "profile": "reader"},
        ])
        members = team["members"]
        first = await repository.create_task(team["team_id"], "first", "inspect")
        second = await repository.create_task(team["team_id"], "second", "summarize", depends_on=[first["task_id"]])
        with pytest.raises(CollaborationConflict):
            await repository.claim_task(team["team_id"], second["task_id"], members[0]["member_id"], 1)
        claimed = await repository.claim_task(team["team_id"], first["task_id"], members[0]["member_id"], 1)
        with pytest.raises(CollaborationConflict):
            await repository.claim_task(team["team_id"], first["task_id"], members[1]["member_id"], 1)
        await repository.finish_task(team["team_id"], first["task_id"], members[0]["member_id"], claimed["version"], "completed", "evidence")
        assert (await repository.claim_task(team["team_id"], second["task_id"], members[1]["member_id"], 1))["status"] == "running"


async def test_team_messages_validate_sender_and_recipient(scope):
    from multiclaw.collaboration.repository import CollaborationNotFound, CollaborationRepository

    database, context = scope
    async with database.write_transaction() as connection:
        repository = CollaborationRepository(connection, context, database.dialect)
        team = await repository.create_team("investigate", [
            {"name": "lead", "role": "leader", "profile": "reader"},
            {"name": "peer", "role": "member", "profile": "reader"},
        ])
        member = team["members"][0]["member_id"]
        message = await repository.send_message(team["team_id"], member, None, "finding")
        assert message["content"] == "finding"
        assert len(await repository.messages(team["team_id"], member)) == 1
        with pytest.raises(CollaborationNotFound):
            await repository.send_message(team["team_id"], member, str(uuid4()), "bad")
