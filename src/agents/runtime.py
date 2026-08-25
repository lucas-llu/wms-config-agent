"""LangGraph runtime factories and a repeatable Day 1 compatibility probe."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class RuntimeProbeState(TypedDict, total=False):
    subject: str
    prepared: bool
    approval_requested: bool
    approved: bool
    result: str


@asynccontextmanager
async def open_async_sqlite_checkpointer(
    database_path: str | Path,
) -> AsyncIterator[AsyncSqliteSaver]:
    """Open a durable asynchronous checkpointer below an explicit file path."""

    path = Path(database_path)
    if not path.name:
        raise ValueError("database_path must identify a SQLite file")
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


def build_runtime_probe_graph(checkpointer: BaseCheckpointSaver[Any]) -> Any:
    """Build a three-node graph used to verify interrupt, stream, and restart behavior."""

    builder = StateGraph(RuntimeProbeState)
    builder.add_node("prepare", _prepare_probe)
    builder.add_node("approval", _request_probe_approval)
    builder.add_node("finalize", _finalize_probe)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")
    builder.add_edge("approval", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer, name="agent-runtime-probe")


async def run_runtime_probe(database_path: str | Path) -> dict[str, Any]:
    """Pause a graph, close it, reopen it, and resume from the saved checkpoint."""

    thread_id = "day1-runtime-probe"
    config = {"configurable": {"thread_id": thread_id}}
    initial_events: list[dict[str, Any]] = []
    resumed_events: list[dict[str, Any]] = []

    async with open_async_sqlite_checkpointer(database_path) as checkpointer:
        graph = build_runtime_probe_graph(checkpointer)
        async for event in graph.astream(
            {"subject": "wms-agent-v2"}, config, stream_mode="updates"
        ):
            initial_events.append(event)
        paused = await graph.aget_state(config)
        if not paused.next or paused.next[0] != "approval":
            raise RuntimeError("runtime probe did not pause at the approval node")

    async with open_async_sqlite_checkpointer(database_path) as checkpointer:
        graph = build_runtime_probe_graph(checkpointer)
        async for event in graph.astream(
            Command(resume={"approved": True}), config, stream_mode="updates"
        ):
            resumed_events.append(event)
        completed = await graph.aget_state(config)

    return {
        "thread_id": thread_id,
        "paused_next": list(paused.next),
        "initial_event_count": len(initial_events),
        "resumed_event_count": len(resumed_events),
        "final_values": dict(completed.values),
        "completed": not completed.next,
    }


def _prepare_probe(state: RuntimeProbeState) -> RuntimeProbeState:
    subject = state.get("subject", "").strip()
    if not subject:
        raise ValueError("runtime probe subject must be non-empty")
    return {"prepared": True}


def _request_probe_approval(state: RuntimeProbeState) -> RuntimeProbeState:
    if not state.get("prepared"):
        raise RuntimeError("runtime probe reached approval before preparation")
    decision = interrupt(
        {
            "kind": "approval",
            "question": "Resume the Day 1 runtime probe?",
            "subject": state["subject"],
        }
    )
    approved = decision.get("approved") is True if isinstance(decision, dict) else decision is True
    return {"approval_requested": True, "approved": approved}


def _finalize_probe(state: RuntimeProbeState) -> RuntimeProbeState:
    result = "approved" if state.get("approved") else "rejected"
    return {"result": result}
