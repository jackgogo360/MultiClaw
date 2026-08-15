import threading

from pydantic import BaseModel

from multiclaw.tools.base import ToolBuilder


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolBuilder[BaseModel]] = {}
        self._lock = threading.RLock()

    def register(self, builder: ToolBuilder[BaseModel]) -> None:
        with self._lock:
            self._tools[builder.name] = builder

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def replace_namespace(self, prefix: str, builders: list[ToolBuilder[BaseModel]]) -> None:
        with self._lock:
            for name in [name for name in self._tools if name.startswith(prefix)]:
                del self._tools[name]
            for builder in builders:
                self._tools[builder.name] = builder

    def get(self, name: str) -> ToolBuilder[BaseModel] | None:
        with self._lock:
            return self._tools.get(name)

    def list_all(self) -> list[ToolBuilder[BaseModel]]:
        with self._lock:
            return [self._tools[name] for name in sorted(self._tools)]

    def clear(self) -> None:
        with self._lock:
            self._tools.clear()

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
