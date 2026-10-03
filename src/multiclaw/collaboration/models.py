"""Validated deployment-bounded collaboration requests."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=12000)
    context: str = Field(default="", max_length=16000)
    profile: Literal["reader", "writer"] = "reader"
    model: str | None = Field(default=None, min_length=1, max_length=255)
    project_path: str = Field(default=".", min_length=1, max_length=1000)
    instructions: str = Field(default="", max_length=8000)


class MemberRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    role: Literal["leader", "member"] = "member"
    profile: Literal["reader", "writer"] = "reader"
    model: str | None = Field(default=None, max_length=255)
    instructions: str = Field(default="", max_length=8000)


class TeamRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=12000)
    members: list[MemberRequest] = Field(min_length=2, max_length=6)
    project_path: str = Field(default=".", min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_members(self):
        if len({member.name for member in self.members}) != len(self.members):
            raise ValueError("member names must be unique")
        if sum(member.role == "leader" for member in self.members) != 1:
            raise ValueError("a team requires exactly one leader")
        return self
