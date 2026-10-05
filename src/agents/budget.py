"""Deterministic per-turn Agent budget enforcement."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.settings import AgentSettings


@dataclass(frozen=True, slots=True)
class BudgetEntry:
    allowed: bool
    update: dict[str, Any]


class TurnBudgetPolicy:
    """Enforce node, retry, wall-clock, and token budgets from checkpoint state."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.clock = clock

    def enter_node(self, state: dict[str, Any], node_name: str) -> BudgetEntry:
        deadline = float(state.get("turn_deadline_epoch", 0) or 0)
        if deadline and self.clock() >= deadline:
            return BudgetEntry(False, self.pause_update(state, "turn_timeout", node_name))
        executed = int(state.get("nodes_executed", 0))
        if executed >= self.settings.max_nodes_per_turn:
            return BudgetEntry(False, self.pause_update(state, "node_budget_exceeded", node_name))
        return BudgetEntry(True, {"nodes_executed": executed + 1})

    def account_llm(
        self,
        state: dict[str, Any],
        *,
        retries: int,
        tokens_used: int,
        node_name: str,
    ) -> BudgetEntry:
        total_retries = int(state.get("retry_count", 0)) + retries
        total_tokens = int(state.get("tokens_used", 0)) + tokens_used
        update = {"retry_count": total_retries, "tokens_used": total_tokens}
        deadline = float(state.get("turn_deadline_epoch", 0) or 0)
        if deadline and self.clock() >= deadline:
            update.update(self.pause_update(state, "turn_timeout", node_name))
            return BudgetEntry(False, update)
        if total_retries > self.settings.max_self_repair_rounds:
            update.update(self.pause_update(state, "retry_budget_exceeded", node_name))
            return BudgetEntry(False, update)
        if total_tokens > self.settings.max_tokens_per_turn:
            update.update(self.pause_update(state, "token_budget_exceeded", node_name))
            return BudgetEntry(False, update)
        return BudgetEntry(True, update)

    @staticmethod
    def pause_update(state: dict[str, Any], reason: str, node_name: str) -> dict[str, Any]:
        return {
            "status": "paused",
            "pause_reason": reason,
            "next_action": "resume_with_new_turn",
            "active_agent": node_name,
            "open_questions": list(state.get("open_questions", [])),
        }
