"""External MCP client integration for the stock agent.

Servers are configured by the operator through ``MCP_SERVERS_JSON``.  Browser
clients can discover the safe, public tool catalogue, but never receive server
URLs, headers, or expanded environment-variable secrets.

Only Streamable HTTP is supported in this first phase.  A fresh MCP session is
opened for discovery and for every tool call, which keeps the manager safe to
use across concurrent FastAPI requests and multiple LangGraph runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit


_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MCP_TOOL_PREFIX = "mcp__"
_URL_IN_ERROR_RE = re.compile(r"https?://[^\s'\"]+", re.IGNORECASE)


class MCPConfigurationError(ValueError):
    """An MCP server entry is malformed or unsafe."""


class MCPToolError(RuntimeError):
    """An MCP tool could not be discovered or executed."""


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: Optional[Tuple[str, ...]] = None
    enabled: bool = True
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class MCPToolInfo:
    server: str
    remote_name: str
    public_name: str
    description: str
    input_schema: Mapping[str, Any]
    read_only: bool = False

    def as_openai_tool(self) -> Dict[str, Any]:
        """Return the function schema accepted by the existing ChatOpenAI client."""
        parameters = dict(self.input_schema or {})
        if parameters.get("type") != "object":
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.public_name,
                "description": (
                    f"[External MCP: {self.server}] "
                    + (self.description or f"Run the {self.remote_name} tool.")
                )[:1024],
                "parameters": parameters,
            },
        }

    def as_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.public_name,
            "name": self.remote_name,
            "description": self.description,
            "server": self.server,
            "read_only": self.read_only,
        }


@dataclass(frozen=True)
class MCPServerSnapshot:
    name: str
    connected: bool
    tools: Tuple[MCPToolInfo, ...] = ()
    error: Optional[str] = None

    def as_public_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "error": self.error,
            "tools": [tool.as_public_dict() for tool in self.tools],
        }


@dataclass(frozen=True)
class MCPToolExecution:
    public_name: str
    server: str
    remote_name: str
    content: str
    is_error: bool = False


def _bool_env(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _number_env(
    name: str,
    default: float,
    *,
    minimum: float,
    integer: bool = False,
) -> float | int:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return int(value) if integer else value


def _substitute_env(value: str, env: Mapping[str, str]) -> str:
    """Expand only explicit ${NAME} references; never apply shell expansion."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        resolved = env.get(key)
        if resolved is None:
            raise MCPConfigurationError(f"missing environment variable {key}")
        return resolved

    return _ENV_REF_RE.sub(replace, value)


def _validate_server_url(url: str, *, allow_insecure_http: bool) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MCPConfigurationError("url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise MCPConfigurationError("credentials must be supplied through headers, not the URL")
    if parsed.fragment:
        raise MCPConfigurationError("url fragments are not supported")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise MCPConfigurationError(
            "plain HTTP is disabled; use HTTPS or set MCP_ALLOW_INSECURE_HTTP=1 for development"
        )

    # A literal private address is almost always an accidental secret-network
    # exposure.  Hostnames remain operator-controlled through MCP_SERVERS_JSON.
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global and not allow_insecure_http:
        raise MCPConfigurationError("private and loopback IP addresses are disabled")
    return url.strip()


def _normalise_entries(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        if not all(isinstance(item, dict) for item in raw):
            raise MCPConfigurationError("every server entry in the array must be an object")
        return [dict(item) for item in raw]
    if isinstance(raw, dict):
        entries: List[Dict[str, Any]] = []
        for name, value in raw.items():
            if not isinstance(value, dict):
                raise MCPConfigurationError(f"server {name!r} must be an object")
            item = dict(value)
            item.setdefault("name", name)
            entries.append(item)
        return entries
    raise MCPConfigurationError("MCP_SERVERS_JSON must be a JSON array or object")


def load_mcp_server_configs(
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[List[MCPServerConfig], List[str]]:
    """Parse server configuration without exposing header values in errors."""
    env = environ if environ is not None else os.environ
    raw_json = (env.get("MCP_SERVERS_JSON") or "").strip()
    if not raw_json:
        return [], []

    try:
        entries = _normalise_entries(json.loads(raw_json))
    except (json.JSONDecodeError, TypeError, MCPConfigurationError) as exc:
        return [], [f"MCP_SERVERS_JSON: {exc}"]

    allow_insecure = _bool_env(env.get("MCP_ALLOW_INSECURE_HTTP"))
    configs: List[MCPServerConfig] = []
    errors: List[str] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        label = str(entry.get("name") or f"#{index + 1}")
        try:
            name = str(entry.get("name") or "").strip()
            if not _SERVER_NAME_RE.fullmatch(name):
                raise MCPConfigurationError("name must match [A-Za-z0-9_-] and be at most 32 chars")
            if name in seen:
                raise MCPConfigurationError("duplicate server name")

            raw_enabled = entry.get("enabled", True)
            if not isinstance(raw_enabled, bool):
                raise MCPConfigurationError("enabled must be true or false")
            enabled = raw_enabled
            url = _validate_server_url(
                str(entry.get("url") or ""),
                allow_insecure_http=allow_insecure,
            )
            raw_headers = entry.get("headers") or {}
            if not isinstance(raw_headers, dict):
                raise MCPConfigurationError("headers must be an object")
            headers: Dict[str, str] = {}
            for key, value in raw_headers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise MCPConfigurationError("header names and values must be strings")
                headers[key] = _substitute_env(value, env)

            # Preferred for .env files: the JSON contains only an environment
            # variable name, while the full secret header value lives separately.
            raw_header_env = entry.get("header_env") or {}
            if not isinstance(raw_header_env, dict):
                raise MCPConfigurationError("header_env must be an object")
            for key, env_name in raw_header_env.items():
                if not isinstance(key, str) or not isinstance(env_name, str):
                    raise MCPConfigurationError("header_env names must be strings")
                if env_name not in env:
                    raise MCPConfigurationError(f"missing environment variable {env_name}")
                headers[key] = env[env_name]

            raw_allowed = entry.get("allowed_tools")
            allowed_tools: Optional[Tuple[str, ...]] = None
            if raw_allowed is not None:
                if not isinstance(raw_allowed, list) or not all(
                    isinstance(item, str) and item.strip() for item in raw_allowed
                ):
                    raise MCPConfigurationError("allowed_tools must be an array of tool names")
                allowed_tools = tuple(dict.fromkeys(item.strip() for item in raw_allowed))

            timeout = float(entry.get("timeout_seconds", env.get("MCP_TIMEOUT_SECONDS", "20")))
            if timeout < 1 or timeout > 120:
                raise MCPConfigurationError("timeout_seconds must be between 1 and 120")

            configs.append(
                MCPServerConfig(
                    name=name,
                    url=url,
                    headers=headers,
                    allowed_tools=allowed_tools,
                    enabled=enabled,
                    timeout_seconds=timeout,
                )
            )
            seen.add(name)
        except (TypeError, ValueError, MCPConfigurationError) as exc:
            errors.append(f"MCP server {label!r}: {exc}")

    return configs, errors


def public_tool_name(server: str, remote_name: str) -> str:
    """Create a stable OpenAI-compatible name, capped at 64 characters."""
    clean_server = _TOOL_NAME_RE.sub("_", server).strip("_") or "server"
    clean_tool = _TOOL_NAME_RE.sub("_", remote_name).strip("_") or "tool"
    candidate = f"{_MCP_TOOL_PREFIX}{clean_server}__{clean_tool}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(f"{server}\0{remote_name}".encode("utf-8")).hexdigest()[:10]
    return f"{candidate[:53]}_{digest}"


def _collision_safe_tool_name(server: str, remote_name: str, used: set[str]) -> str:
    candidate = public_tool_name(server, remote_name)
    if candidate not in used:
        return candidate
    digest = hashlib.sha256(f"{server}\0{remote_name}".encode("utf-8")).hexdigest()[:10]
    return f"{candidate[:53]}_{digest}"


def _safe_exception_text(exc: Exception) -> str:
    """Keep diagnostics useful without exposing configured endpoint URLs."""
    return _URL_IN_ERROR_RE.sub("[MCP endpoint]", str(exc))[:300]


def _tool_read_only(tool: Any) -> bool:
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    value = getattr(annotations, "read_only_hint", None)
    if value is None and isinstance(annotations, dict):
        value = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    return bool(value)


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return value
    return value


def _result_content(result: Any, max_chars: int) -> Tuple[str, bool]:
    """Flatten an MCP result to bounded text suitable for a ToolMessage."""
    blocks: List[str] = []
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        blocks.append(json.dumps(structured, ensure_ascii=False, default=str))

    for raw_block in getattr(result, "content", None) or []:
        block = _model_dump(raw_block)
        if not isinstance(block, dict):
            blocks.append(str(block))
            continue
        block_type = str(block.get("type") or "content")
        if isinstance(block.get("text"), str):
            blocks.append(block["text"])
        elif block_type in {"image", "audio"}:
            blocks.append(f"[{block_type} content omitted from text context]")
        elif isinstance(block.get("resource"), dict):
            resource = block["resource"]
            text = resource.get("text")
            blocks.append(text if isinstance(text, str) else json.dumps(resource, ensure_ascii=False, default=str))
        else:
            blocks.append(json.dumps(block, ensure_ascii=False, default=str))

    content = "\n\n".join(part for part in blocks if part).strip() or "MCP tool returned no content."
    if len(content) > max_chars:
        content = content[: max_chars - 1] + "…"
    return content, bool(getattr(result, "is_error", False))


class MCPClientManager:
    def __init__(self) -> None:
        self._configs, self._config_errors = load_mcp_server_configs()
        self._snapshots: Tuple[MCPServerSnapshot, ...] = ()
        self._tools: Dict[str, MCPToolInfo] = {}
        self._last_refresh = 0.0
        self._refresh_lock = asyncio.Lock()
        self._cache_ttl = _number_env(
            "MCP_TOOL_CACHE_TTL_SECONDS", 300.0, minimum=5.0
        )
        self._max_tools_per_server = _number_env(
            "MCP_MAX_TOOLS_PER_SERVER", 24, minimum=1, integer=True
        )
        self._max_output_chars = _number_env(
            "MCP_MAX_OUTPUT_CHARS", 12000, minimum=1000, integer=True
        )

    @property
    def configured(self) -> bool:
        return any(config.enabled for config in self._configs)

    @property
    def config_errors(self) -> Tuple[str, ...]:
        return tuple(self._config_errors)

    def reload_config(self) -> None:
        """Reload operator configuration; the next request performs discovery."""
        self._configs, self._config_errors = load_mcp_server_configs()
        self._snapshots = ()
        self._tools = {}
        self._last_refresh = 0.0

    @asynccontextmanager
    async def _open_client(self, config: MCPServerConfig) -> AsyncIterator[Any]:
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:  # pragma: no cover - dependency check is deployment-specific
            raise MCPToolError("MCP client dependency is not installed") from exc

        timeout = httpx2.Timeout(
            connect=config.timeout_seconds,
            read=config.timeout_seconds,
            write=config.timeout_seconds,
            pool=config.timeout_seconds,
        )
        async with httpx2.AsyncClient(
            headers=dict(config.headers),
            timeout=timeout,
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(config.url, http_client=http_client)
            async with Client(
                transport,
                read_timeout_seconds=config.timeout_seconds,
            ) as client:
                yield client

    async def _discover_one(self, config: MCPServerConfig) -> MCPServerSnapshot:
        try:
            discovered: List[MCPToolInfo] = []
            used_public_names: set[str] = set()
            allowed = set(config.allowed_tools) if config.allowed_tools is not None else None
            async with asyncio.timeout(config.timeout_seconds):
                async with self._open_client(config) as client:
                    cursor: Optional[str] = None
                    for _ in range(10):
                        page = await client.list_tools(cursor=cursor)
                        for tool in page.tools:
                            remote_name = str(tool.name)
                            if allowed is not None and remote_name not in allowed:
                                continue
                            read_only = _tool_read_only(tool)
                            # When the server does not declare a tool read-only, the
                            # operator must explicitly opt in through allowed_tools.
                            if allowed is None and not read_only:
                                continue
                            schema = getattr(tool, "input_schema", None) or {
                                "type": "object",
                                "properties": {},
                            }
                            safe_name = _collision_safe_tool_name(
                                config.name,
                                remote_name,
                                used_public_names,
                            )
                            used_public_names.add(safe_name)
                            discovered.append(
                                MCPToolInfo(
                                    server=config.name,
                                    remote_name=remote_name,
                                    public_name=safe_name,
                                    description=str(getattr(tool, "description", "") or ""),
                                    input_schema=dict(schema),
                                    read_only=read_only,
                                )
                            )
                            if len(discovered) >= self._max_tools_per_server:
                                cursor = None
                                break
                        else:
                            cursor = getattr(page, "next_cursor", None)
                        if not cursor or len(discovered) >= self._max_tools_per_server:
                            break
            return MCPServerSnapshot(config.name, True, tuple(discovered))
        except Exception as exc:
            return MCPServerSnapshot(
                config.name,
                False,
                error=f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            )

    async def refresh(self, *, force: bool = False) -> Tuple[MCPServerSnapshot, ...]:
        # Supports direct module execution where chat.py calls load_dotenv only
        # after importing this manager.  Normal FastAPI startup already loads it.
        if not self._configs and os.getenv("MCP_SERVERS_JSON"):
            self.reload_config()
        if not self.configured:
            self._snapshots = ()
            self._tools = {}
            return self._snapshots
        now = time.monotonic()
        if not force and self._snapshots and now - self._last_refresh < self._cache_ttl:
            return self._snapshots

        async with self._refresh_lock:
            now = time.monotonic()
            if not force and self._snapshots and now - self._last_refresh < self._cache_ttl:
                return self._snapshots
            active = [config for config in self._configs if config.enabled]
            snapshots = await asyncio.gather(*(self._discover_one(config) for config in active))
            self._snapshots = tuple(snapshots)
            self._tools = {
                tool.public_name: tool
                for snapshot in snapshots
                for tool in snapshot.tools
            }
            self._last_refresh = time.monotonic()
            return self._snapshots

    async def openai_tools(self) -> List[Dict[str, Any]]:
        await self.refresh()
        return [tool.as_openai_tool() for tool in self._tools.values()]

    async def tool_names(self) -> List[str]:
        await self.refresh()
        return list(self._tools)

    async def public_status(self, *, force: bool = False) -> Dict[str, Any]:
        snapshots = await self.refresh(force=force)
        return {
            "configured": self.configured,
            "config_errors": list(self._config_errors),
            "servers": [snapshot.as_public_dict() for snapshot in snapshots],
        }

    async def call_tool(self, public_name: str, arguments: Mapping[str, Any]) -> MCPToolExecution:
        await self.refresh()
        tool = self._tools.get(public_name)
        if tool is None:
            raise MCPToolError(f"unknown or disabled MCP tool: {public_name}")
        config = next((item for item in self._configs if item.name == tool.server), None)
        if config is None or not config.enabled:
            raise MCPToolError(f"MCP server is disabled: {tool.server}")

        try:
            async with asyncio.timeout(config.timeout_seconds):
                async with self._open_client(config) as client:
                    result = await client.call_tool(
                        tool.remote_name,
                        dict(arguments),
                        read_timeout_seconds=config.timeout_seconds,
                    )
            content, is_error = _result_content(result, self._max_output_chars)
            return MCPToolExecution(
                public_name=public_name,
                server=tool.server,
                remote_name=tool.remote_name,
                content=content,
                is_error=is_error,
            )
        except MCPToolError:
            raise
        except Exception as exc:
            raise MCPToolError(
                f"MCP tool {tool.server}/{tool.remote_name} failed: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}"
            ) from exc


mcp_client_manager = MCPClientManager()


def is_mcp_tool_name(name: str) -> bool:
    return isinstance(name, str) and name.startswith(_MCP_TOOL_PREFIX)
