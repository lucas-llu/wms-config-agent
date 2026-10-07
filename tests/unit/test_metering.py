"""No vendor billing calls: token subsets, provenance, month boundaries and decimals."""

from datetime import UTC, datetime

import pytest

from multiuser.metering import (
    Usage,
    billing_period,
    estimated_cost,
    normalize_usage,
    validate_price,
)

PRICE = {
    "version": "fixture-v1",
    "currency": "USD",
    "input": "2",
    "cached": "0.5",
    "output": "4",
    "confirmed": True,
}


def test_provider_subsets_are_not_double_counted():
    usage = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert usage == Usage("provider", 100, 50, 150, 40, 20)
    assert estimated_cost(usage, PRICE) == "0.000340000000"


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 4},
        {"prompt_tokens": 3, "completion_tokens": 1, "prompt_cache_hit_tokens": 4},
        {"prompt_tokens": 3, "completion_tokens": 1, "reasoning_tokens": 2},
        {"total_tokens": True},
        {"total_tokens": -1},
        {"prompt_tokens": "100"},
    ],
)
def test_malformed_usage_is_unknown_not_zero(raw):
    usage = normalize_usage(raw)
    assert usage.source == "unknown"
    assert usage.total_tokens is None
    assert estimated_cost(usage, PRICE) is None


def test_partial_total_is_actual_but_unpriced_and_missing_usage_is_estimated():
    assert normalize_usage({"total_tokens": 12}) == Usage("provider", None, None, 12)
    assert estimated_cost(normalize_usage({"total_tokens": 12}), PRICE) is None
    assert normalize_usage(None, input_estimate=100, output_estimate=30) == Usage(
        "estimated", 100, 30, 130
    )
    assert estimated_cost(Usage("provider", 100, 20, 120), PRICE) is None
    assert estimated_cost(Usage("provider", 100, 20, 120, 0), None) is None


def test_alternative_input_output_usage_and_shanghai_utc_boundary():
    assert normalize_usage(
        {"input_tokens": 5, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 2}}
    ) == Usage("provider", 5, 3, 8, 2)
    assert str(billing_period(datetime(2026, 9, 30, 15, 59, tzinfo=UTC))) == "2026-09-01"
    assert str(billing_period(datetime(2026, 9, 30, 16, 0, tzinfo=UTC))) == "2026-10-01"
    with pytest.raises(ValueError):
        billing_period(datetime(2026, 10, 1))


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": False},
        {"input": -1},
        {"input": "NaN"},
        {"cached": "-1"},
        {"currency": "???"},
        {"version": ""},
        {"extra": 0},
    ],
)
def test_unconfirmed_invalid_or_float_prices_are_rejected(changes):
    with pytest.raises(ValueError):
        validate_price({**PRICE, **changes})
