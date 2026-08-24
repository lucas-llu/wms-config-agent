from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from libs.llm import BudgetedLLM, ChatResponse, LLMBudgetExceeded


class FakeLLM:
    model = "fake-model"

    @staticmethod
    def chat(messages, trace=None) -> ChatResponse:
        return ChatResponse(messages[-1]["content"], model="fake-model")


def test_budgeted_llm_enforces_shared_logical_call_limit() -> None:
    llm = BudgetedLLM(FakeLLM(), max_calls=2)

    assert llm.chat([{"role": "user", "content": "one"}]).content == "one"
    assert llm.chat([{"role": "user", "content": "two"}]).content == "two"
    with pytest.raises(LLMBudgetExceeded, match="after 2 calls"):
        llm.chat([{"role": "user", "content": "three"}])

    assert llm.calls_made == 2
    assert llm.remaining_calls == 0
    assert llm.model == "fake-model"


def test_budget_reservation_is_thread_safe() -> None:
    llm = BudgetedLLM(FakeLLM(), max_calls=5)

    def invoke(index: int) -> bool:
        try:
            llm.chat([{"role": "user", "content": str(index)}])
            return True
        except LLMBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(invoke, range(20)))

    assert outcomes.count(True) == 5
    assert outcomes.count(False) == 15
    assert llm.calls_made == 5


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        BudgetedLLM(FakeLLM(), max_calls=0)
