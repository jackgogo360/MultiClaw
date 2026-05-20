from pydantic import BaseModel

from multiclaw.tools.base import ToolBuilder


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolBuilder[BaseModel]] = {}

    def register(self, builder: ToolBuilder[BaseModel]) -> None:
        self._tools[builder.name] = builder

    def get(self, name: str) -> ToolBuilder[BaseModel] | None:
        return self._tools.get(name)

    def list_all(self) -> list[ToolBuilder[BaseModel]]:
        return [self._tools[name] for name in sorted(self._tools)]

    def to_openai_schemas(self) -> list[dict]:
        schemas: list[dict] = []
        for builder in self.list_all():
            if hasattr(builder, "to_openai_schema"):
                schema = builder.to_openai_schema()  # type: ignore[operator]
            else:
                json_schema = builder.parameters_schema.model_json_schema()
                schema = {
                    "type": "function",
                    "function": {
                        "name": builder.name,
                        "description": builder.description,
                        "parameters": json_schema,
                    },
                }
            schemas.append(schema)
        return schemas
