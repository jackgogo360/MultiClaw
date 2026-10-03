"""Member approvals may authorize operations, never expand their workspace."""
from multiclaw.governance.permission import PermissionChecker
from multiclaw.governance.models import PermissionDecision


class MemberPermissionChecker(PermissionChecker):
    async def check(self, tool_name, raw_params=None, workspace_root=None):
        decision = await super().check(tool_name, raw_params, workspace_root)
        if decision.approved_roots:
            return PermissionDecision(allow=False, requires_approval=False,
                                      reason="member_workspace_escape_denied")
        return decision
