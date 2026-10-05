from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runtime import run_runtime_probe


def test_runtime_probe_streams_interrupts_restarts_and_resumes(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "agent" / "probe.db"

    report = asyncio.run(run_runtime_probe(checkpoint_path))

    assert checkpoint_path.is_file()
    assert report["paused_next"] == ["approval"]
    assert report["initial_event_count"] >= 2
    assert report["resumed_event_count"] >= 2
    assert report["completed"] is True
    assert report["final_values"] == {
        "subject": "wms-agent-v2",
        "prepared": True,
        "approval_requested": True,
        "approved": True,
        "result": "approved",
    }
