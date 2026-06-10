from omnidesk_agent.tools.registry import ToolRegistry


class DummyTool:
    name = "dummy"

    async def call(self, action, args, ctx):
        raise NotImplementedError


def test_tool_registry_describe_has_fallback_spec():
    reg = ToolRegistry()
    reg.register(DummyTool())
    desc = reg.describe()
    assert "dummy" in desc
    assert "actions" in desc["dummy"]
