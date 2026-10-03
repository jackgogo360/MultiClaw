from uuid import uuid4
import httpx
from fastapi import FastAPI
from test_collaboration_store import scope
from multiclaw.config import Settings


async def test_collaboration_api_handles_teams_jobs_and_scope_denial(scope):
    from multiclaw.api.collaboration import router
    from multiclaw.api.dependencies import tenant_context
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope

    async def executor(job, parent):
        return {"status": "completed", "summary": "done"}

    service = CollaborationService(database, executor=executor)
    app = FastAPI()
    app.include_router(router)
    app.state.database = database
    app.state.settings = Settings(_config_file="/nonexistent", collaboration={"enabled": True})
    app.state.collaboration = service
    app.dependency_overrides[tenant_context] = lambda: context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/agents", json={"session_id": context.session_id, "goal": "inspect"})
        assert response.status_code == 200
        job = response.json()
        assert job["status"] == "queued"
        assert (await client.get(f"/api/agents/{job['job_id']}?session_id={uuid4()}")).status_code == 404
        assert (await client.get(f"/api/agents/{uuid4()}?session_id={context.session_id}")).status_code == 404
        response = await client.post("/api/teams", json={"session_id": context.session_id,
            "objective": "investigate", "members": [{"name": "lead", "role": "leader"}, {"name": "peer"}]})
        assert response.status_code == 200
        team = response.json()
        recipient = team["members"][1]["member_id"]
        response = await client.post(f"/api/teams/{team['team_id']}/messages", json={
            "session_id": context.session_id, "content": "hello", "recipient_id": recipient,
        })
        assert response.status_code == 200 and response.json()["sender_id"] is None
        response = await client.post(f"/api/teams/{team['team_id']}/tasks", json={
            "session_id": context.session_id, "title": "inspect", "objective": "read files",
            "assigned_member_id": recipient,
        })
        assert response.status_code == 200
        response = await client.get(f"/api/collaboration?session_id={context.session_id}")
        assert response.status_code == 200 and len(response.json()["jobs"]) == 2
    await service.close()


async def test_team_change_acceptance_is_scoped_digest_bound_and_idempotent(scope):
    from multiclaw.api.collaboration import router
    from multiclaw.api.dependencies import tenant_context
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    digest = "a" * 64
    accepted = []

    class Manager:
        async def diff_batch(self, workspaces):
            assert len(workspaces) == 1
            return {"patch": "reviewed patch", "files": ["result.txt"], "digest": digest}

        async def accept_batch(self, workspaces, expected):
            if expected != digest:
                raise ValueError("stale digest")
            accepted.append(expected)
            return {"accepted": True, "digest": expected}

    class Executor:
        def manager(self, parent):
            return Manager()

    service = CollaborationService(database, executor=Executor())
    async with service.store(context, write=True) as repository:
        team = await repository.create_team("deliver", [{"name": "lead", "role": "leader"}, {"name": "peer", "profile": "writer"}])
        job = await repository.create_job({"goal": "write", "profile": "writer"}, team_id=team["team_id"], member_id=team["members"][1]["member_id"])
        await repository.update_job(job["job_id"], job["version"], status="completed", result={"workspace": {"job_id": job["job_id"]}})
        await repository.update_team(team["team_id"], team["version"], status="completed")
    app = FastAPI()
    app.include_router(router)
    app.state.database = database
    app.state.settings = Settings(_config_file="/nonexistent", collaboration={"enabled": True})
    app.state.collaboration = service
    app.dependency_overrides[tenant_context] = lambda: context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        prefix = f"/api/teams/{team['team_id']}"
        assert (await client.get(f"{prefix}/changes?session_id={context.session_id}")).json()["digest"] == digest
        assert (await client.post(f"/api/agents/{job['job_id']}/accept", json={"session_id": context.session_id, "digest": digest})).status_code == 409
        assert (await client.post(prefix + "/accept", json={"session_id": context.session_id, "digest": "b" * 64})).status_code == 422
        for _ in range(2):
            assert (await client.post(prefix + "/accept", json={"session_id": context.session_id, "digest": digest})).status_code == 200
        assert accepted == [digest]
        assert (await client.get(f"{prefix}/changes?session_id={uuid4()}")).status_code == 404
    await service.close()
