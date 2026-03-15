import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


class StubProvider:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.generation = MagicMock(max_tokens=4096, temperature=0.1, reasoning_effort=None)

    def get_default_model(self) -> str:
        return "test-model"

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        return self._responses.pop(0)


class TrackingTool(Tool):
    def __init__(self, name: str, delay: float = 0.0):
        self._name = name
        self._delay = delay
        self.max_active = 0
        self._active = 0
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        }

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return kwargs.get("value", self._name)
        finally:
            self._active -= 1


class IdTool(TrackingTool):
    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return "ID: abc123"


def _make_loop(tmp_path: Path, responses: list[LLMResponse]) -> AgentLoop:
    provider = StubProvider(responses)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        model="test-model",
    )
    loop.memory_consolidator.maybe_consolidate_by_tokens = MagicMock()  # type: ignore[method-assign]
    return loop


@pytest.mark.asyncio
async def test_run_agent_loop_executes_ready_tools_in_parallel(tmp_path) -> None:
    loop = _make_loop(
        tmp_path,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="a", name="slow_a", arguments={"value": "A"}),
                    ToolCallRequest(id="b", name="slow_b", arguments={"value": "B"}),
                ],
            ),
            LLMResponse(content="done"),
        ],
    )
    slow_a = TrackingTool("slow_a", delay=0.05)
    slow_b = TrackingTool("slow_b", delay=0.05)
    loop.tools._tools = {"slow_a": slow_a, "slow_b": slow_b}

    final_content, tools_used, _ = await loop._run_agent_loop([{"role": "user", "content": "hi"}])

    assert final_content == "done"
    assert tools_used == ["slow_a", "slow_b"]
    assert slow_a.max_active == 1
    assert slow_b.max_active == 1
    assert slow_a.calls == [{"value": "A"}]
    assert slow_b.calls == [{"value": "B"}]


@pytest.mark.asyncio
async def test_run_agent_loop_parallel_tools_overlap(tmp_path) -> None:
    loop = _make_loop(
        tmp_path,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="a", name="slow_a", arguments={"value": "A"}),
                    ToolCallRequest(id="b", name="slow_b", arguments={"value": "B"}),
                ],
            ),
            LLMResponse(content="done"),
        ],
    )
    gate = {"active": 0, "max": 0}

    class OverlapTool(TrackingTool):
        async def execute(self, **kwargs):
            gate["active"] += 1
            gate["max"] = max(gate["max"], gate["active"])
            try:
                await asyncio.sleep(0.05)
                return kwargs["value"]
            finally:
                gate["active"] -= 1

    loop.tools._tools = {
        "slow_a": OverlapTool("slow_a"),
        "slow_b": OverlapTool("slow_b"),
    }

    await loop._run_agent_loop([{"role": "user", "content": "hi"}])

    assert gate["max"] == 2


@pytest.mark.asyncio
async def test_run_agent_loop_resolves_placeholder_dependencies(tmp_path) -> None:
    loop = _make_loop(
        tmp_path,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="create", name="create_id", arguments={}),
                    ToolCallRequest(id="send", name="send_id", arguments={"value": "<create>"}),
                ],
            ),
            LLMResponse(content="done"),
        ],
    )
    create = IdTool("create_id")
    send = TrackingTool("send_id")
    loop.tools._tools = {"create_id": create, "send_id": send}

    final_content, tools_used, _ = await loop._run_agent_loop([{"role": "user", "content": "hi"}])

    assert final_content == "done"
    assert tools_used == ["create_id", "send_id"]
    assert send.calls == [{"value": "abc123"}]


@pytest.mark.asyncio
async def test_run_agent_loop_executes_pattern_tool_calls(tmp_path) -> None:
    loop = _make_loop(
        tmp_path,
        [
            LLMResponse(
                content='TOOL_CALL: {"id":"p1","name":"echo","arguments":{"value":"hello"}}',
            ),
            LLMResponse(content="done"),
        ],
    )
    echo = TrackingTool("echo")
    loop.tools._tools = {"echo": echo}

    final_content, tools_used, _ = await loop._run_agent_loop([{"role": "user", "content": "hi"}])

    assert final_content == "done"
    assert tools_used == ["echo"]
    assert echo.calls == [{"value": "hello"}]
