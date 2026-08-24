import argparse

import pytest

from scripts.process_corpus import _positive_int, _require_bounded_llm_run


def test_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="greater than 0"):
        _positive_int("0")


def test_llm_ingestion_requires_both_safety_bounds() -> None:
    with pytest.raises(SystemExit, match="--max-documents and --max-llm-calls"):
        _require_bounded_llm_run(
            llm_enabled=True,
            max_documents=2,
            max_llm_calls=None,
        )


def test_local_rule_run_does_not_require_llm_bounds() -> None:
    _require_bounded_llm_run(
        llm_enabled=False,
        max_documents=None,
        max_llm_calls=None,
    )
