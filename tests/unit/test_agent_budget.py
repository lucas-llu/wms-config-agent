from __future__ import annotations

from dataclasses import replace

import pytest

from agents.budget import TurnBudgetPolicy
from agents.contracts import SessionStatus
from agents.graph import transition_status
from core.settings import load_settings


def test_node_budget_pauses_before_executing_extra_node() -> None:
    settings = replace(load_settings().agent, max_nodes_per_turn=1)
    policy = TurnBudgetPolicy(settings, clock=lambda: 10.0)

    assert policy.enter_node({"nodes_executed": 0, "turn_deadline_epoch": 20.0}, "first").allowed
    blocked = policy.enter_node({"nodes_executed": 1, "turn_deadline_epoch": 20.0}, "second")

    assert blocked.allowed is False
    assert blocked.update["status"] == "paused"
    assert blocked.update["pause_reason"] == "node_budget_exceeded"


def test_time_budget_pauses_expired_turn() -> None:
    policy = TurnBudgetPolicy(load_settings().agent, clock=lambda: 20.0)

    blocked = policy.enter_node({"nodes_executed": 0, "turn_deadline_epoch": 20.0}, "node")

    assert blocked.allowed is False
    assert blocked.update["pause_reason"] == "turn_timeout"


def test_time_budget_is_rechecked_after_llm_call() -> None:
    times = iter((10.0, 21.0))
    policy = TurnBudgetPolicy(load_settings().agent, clock=lambda: next(times))
    state = {"nodes_executed": 0, "turn_deadline_epoch": 20.0}

    assert policy.enter_node(state, "requirement").allowed is True
    blocked = policy.account_llm(state, retries=0, tokens_used=5, node_name="requirement")

    assert blocked.allowed is False
    assert blocked.update["pause_reason"] == "turn_timeout"


def test_token_and_retry_budgets_are_accounted() -> None:
    settings = replace(
        load_settings().agent,
        max_tokens_per_turn=10,
        max_self_repair_rounds=1,
    )
    policy = TurnBudgetPolicy(settings)

    token_blocked = policy.account_llm(
        {"tokens_used": 5, "retry_count": 0},
        retries=0,
        tokens_used=6,
        node_name="requirement",
    )
    retry_blocked = policy.account_llm(
        {"tokens_used": 0, "retry_count": 1},
        retries=1,
        tokens_used=1,
        node_name="requirement",
    )

    assert token_blocked.update["pause_reason"] == "token_budget_exceeded"
    assert retry_blocked.update["pause_reason"] == "retry_budget_exceeded"


def test_session_transition_table_rejects_skipping_requirement_collection() -> None:
    with pytest.raises(ValueError, match="created -> planning"):
        transition_status({"status": SessionStatus.CREATED.value}, SessionStatus.PLANNING)
