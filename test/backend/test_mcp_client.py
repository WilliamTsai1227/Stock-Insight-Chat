import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.backend.tools.mcp_client import (
    MCPClientManager,
    MCPServerConfig,
    MCPServerSnapshot,
    MCPToolInfo,
    _result_content,
    _safe_exception_text,
    load_mcp_server_configs,
    public_tool_name,
)


def test_load_config_uses_separate_secret_environment_variable():
    configs, errors = load_mcp_server_configs({
        "MCP_SERVERS_JSON": (
            '{"market":{"url":"https://mcp.example.com/mcp",'
            '"header_env":{"Authorization":"MARKET_AUTH"},'
            '"allowed_tools":["quote"]}}'
        ),
        "MARKET_AUTH": "Bearer secret-token",
    })

    assert errors == []
    assert len(configs) == 1
    assert configs[0].name == "market"
    assert configs[0].headers == {"Authorization": "Bearer secret-token"}
    assert configs[0].allowed_tools == ("quote",)


def test_plain_http_requires_explicit_development_override():
    raw = '{"local":{"url":"http://127.0.0.1:9000/mcp"}}'
    configs, errors = load_mcp_server_configs({"MCP_SERVERS_JSON": raw})
    assert configs == []
    assert "plain HTTP is disabled" in errors[0]

    configs, errors = load_mcp_server_configs({
        "MCP_SERVERS_JSON": raw,
        "MCP_ALLOW_INSECURE_HTTP": "1",
    })
    assert errors == []
    assert configs[0].url == "http://127.0.0.1:9000/mcp"


def test_public_tool_name_is_openai_compatible_and_bounded():
    short = public_tool_name("market", "get.quote/latest")
    long = public_tool_name("market", "x" * 100)

    assert short == "mcp__market__get_quote_latest"
    assert len(long) <= 64
    assert all(char.isalnum() or char in "_-" for char in long)


def test_result_content_keeps_text_and_structured_data_but_omits_binary():
    result = SimpleNamespace(
        structured_content={"price": 123.5},
        content=[
            {"type": "text", "text": "latest quote"},
            {"type": "image", "data": "a" * 1000},
        ],
        is_error=False,
    )

    content, is_error = _result_content(result, 500)
    assert '"price": 123.5' in content
    assert "latest quote" in content
    assert "image content omitted" in content
    assert "a" * 100 not in content
    assert is_error is False


def test_errors_do_not_expose_server_url():
    text = _safe_exception_text(
        RuntimeError("connect failed: https://secret.example.com/mcp?token=abc")
    )
    assert "secret.example.com" not in text
    assert "token=abc" not in text
    assert "[MCP endpoint]" in text


def test_manager_builds_public_catalog_without_connection_details(monkeypatch):
    monkeypatch.setenv(
        "MCP_SERVERS_JSON",
        '{"market":{"url":"https://secret.example.com/mcp"}}',
    )
    manager = MCPClientManager()
    tool = MCPToolInfo(
        server="market",
        remote_name="quote",
        public_name="mcp__market__quote",
        description="Get a quote",
        input_schema={"type": "object", "properties": {"symbol": {"type": "string"}}},
        read_only=True,
    )

    async def fake_discover(_config):
        return MCPServerSnapshot("market", True, (tool,))

    monkeypatch.setattr(manager, "_discover_one", fake_discover)
    status = asyncio.run(manager.public_status(force=True))

    assert status["servers"][0]["tools"][0]["id"] == "mcp__market__quote"
    assert "url" not in status["servers"][0]
    assert "headers" not in status["servers"][0]


def test_discovery_defaults_to_read_only_unless_operator_allowlists(monkeypatch):
    manager = MCPClientManager()
    read_tool = SimpleNamespace(
        name="lookup",
        description="Read data",
        input_schema={"type": "object", "properties": {}},
        annotations=SimpleNamespace(read_only_hint=True),
    )
    write_tool = SimpleNamespace(
        name="delete_record",
        description="Delete data",
        input_schema={"type": "object", "properties": {}},
        annotations=SimpleNamespace(read_only_hint=False),
    )

    class FakeClient:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[read_tool, write_tool], next_cursor=None)

    @asynccontextmanager
    async def fake_open(_config):
        yield FakeClient()

    monkeypatch.setattr(manager, "_open_client", fake_open)
    safe_config = MCPServerConfig("market", "https://example.com/mcp")
    safe_snapshot = asyncio.run(manager._discover_one(safe_config))
    assert [tool.remote_name for tool in safe_snapshot.tools] == ["lookup"]

    explicit_config = MCPServerConfig(
        "market",
        "https://example.com/mcp",
        allowed_tools=("delete_record",),
    )
    explicit_snapshot = asyncio.run(manager._discover_one(explicit_config))
    assert [tool.remote_name for tool in explicit_snapshot.tools] == ["delete_record"]
