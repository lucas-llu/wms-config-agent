"""Synthetic model must follow the same citation/language contract as production."""

import pytest

from agents.supervisor import Supervisor
from agents.workspace import Workspace
from core.settings import load_settings
from scripts.p2_fixture_app import SyntheticKnowledge, SyntheticModel


@pytest.mark.parametrize(
    "question,language", [("What is SYN_MODE?", "en"), ("SYN_MODE 是什么？", "zh")]
)
def test_synthetic_answer_passes_the_real_grounded_answer_contract(question, language):
    supervisor = Supervisor(
        llm=SyntheticModel(),
        knowledge_adapter=SyntheticKnowledge(),
        settings=load_settings().agent,
        workspace=Workspace("workspace:p2", "P2", ("fixture",), ("inbound",), ("DC01",), ("test",)),
    )
    result = supervisor.graph._answer_question(
        {
            "status": "created",
            "intent": "atomic_query",
            "latest_user_message": question,
            "response_language": language,
            "confirmed_context": {},
        }
    )
    assert result["answer_status"] == "answered"
    assert result["answer_evidence"]
