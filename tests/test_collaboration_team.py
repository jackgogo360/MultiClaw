import asyncio
from test_collaboration_store import scope


async def test_team_leader_delegates_peers_message_and_synthesizes(scope):
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.collaboration.tools import register_member_tools
    from multiclaw.tools import ToolRegistry, ToolStatus

    database, context = scope
    observed = []
    service = None

    async def execute(job, parent):
        registry = ToolRegistry()
        register_member_tools(registry, service, parent, job)
        if job["request"]["goal"].startswith("Produce the final team report"):
            return {"status": "completed", "summary": "Team evidence verified"}
        if job["task_id"] is None:
            team = await service.get_team(parent, job["team_id"])
            peer = next(member for member in team["members"] if member["role"] == "member")
            tool = registry.get("team_create_task")
            outcome = await tool.build(tool.validate({"title": "research", "objective": "read evidence",
                "assigned_member_id": peer["member_id"]})).execute()
            assert outcome.status is ToolStatus.SUCCESS
            tool = registry.get("team_message")
            outcome = await tool.build(tool.validate({"content": "Use primary evidence", "recipient_id": peer["member_id"]})).execute()
            assert outcome.status is ToolStatus.SUCCESS
            return {"status": "completed", "summary": "Tasks assigned"}
        observed.append(job["task_id"])
        tool = registry.get("team_inbox")
        outcome = await tool.build(tool.validate({})).execute()
        assert "Use primary evidence" in outcome.content
        tool = registry.get("team_create_task")
        outcome = await tool.build(tool.validate({"title": "unauthorized", "objective": "extra"})).execute()
        assert outcome.status is ToolStatus.ERROR
        return {"status": "completed", "summary": "Evidence found"}

    service = CollaborationService(database, executor=execute)
    team = await service.create_team(context, {"objective": "investigate", "members": [
        {"name": "lead", "role": "leader"}, {"name": "peer"},
    ]})
    for _ in range(30):
        await service.tick()
        await asyncio.sleep(0.03)
        snapshot = await service.get_team(context, team["team_id"])
        if snapshot["status"] == "completed":
            break
    assert snapshot["status"] == "completed"
    assert snapshot["summary"] == "Team evidence verified"
    assert len(snapshot["tasks"]) == 1
    assert snapshot["tasks"][0]["status"] == "completed"
    assert observed == [snapshot["tasks"][0]["task_id"]]
    await service.close()
