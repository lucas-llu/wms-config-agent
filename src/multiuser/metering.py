"""Provider-neutral token provenance and explicit versioned decimal pricing."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def billing_period(at=None):
    at = at or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("UTC-aware timestamp required")
    return at.astimezone(SHANGHAI).date().replace(day=1)


def count(value):
    return value if type(value) is int and 0 <= value <= 1000000000 else None


@dataclass(frozen=True)
class Usage:
    source: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None


def normalize_usage(raw, *, input_estimate=0, output_estimate=0):
    if not isinstance(raw, dict) or not raw:
        return Usage("estimated", input_estimate, output_estimate, input_estimate + output_estimate)
    incoming = count(raw.get("prompt_tokens", raw.get("input_tokens")))
    outgoing = count(raw.get("completion_tokens", raw.get("output_tokens")))
    total = count(raw.get("total_tokens"))
    details = raw.get("prompt_tokens_details", raw.get("input_tokens_details", {}))
    cache = count(
        raw.get(
            "prompt_cache_hit_tokens",
            details.get("cached_tokens") if isinstance(details, dict) else None,
        )
    )
    out_details = raw.get("completion_tokens_details", raw.get("output_tokens_details", {}))
    reasoning = count(
        raw.get(
            "reasoning_tokens",
            out_details.get("reasoning_tokens") if isinstance(out_details, dict) else None,
        )
    )
    if incoming is not None and outgoing is not None:
        if total is not None and total != incoming + outgoing:
            return Usage("unknown", None, None, None)
        total = incoming + outgoing
    if cache is not None and (incoming is None or cache > incoming):
        return Usage("unknown", None, None, None)
    if reasoning is not None and (outgoing is None or reasoning > outgoing):
        return Usage("unknown", None, None, None)
    return Usage(
        "provider" if total is not None else "unknown", incoming, outgoing, total, cache, reasoning
    )


def validate_price(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "version",
        "currency",
        "input",
        "cached",
        "output",
        "confirmed",
    }:
        raise ValueError("Explicit complete price version required")
    if (
        value["confirmed"] is not True
        or not isinstance(value["version"], str)
        or not 1 <= len(value["version"]) <= 80
    ):
        raise ValueError("Unconfirmed price must not be used")
    if value["currency"] not in {"CNY", "USD", "EUR", "GBP", "JPY"}:
        raise ValueError("Unsupported price currency")
    for name in ("input", "cached", "output"):
        if not isinstance(value[name], str):
            raise ValueError("Decimal rates must be strings")
        try:
            rate = Decimal(value[name])
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal rate") from exc
        if not rate.is_finite() or not 0 <= rate <= 1000000:
            raise ValueError("Invalid decimal rate")
    return dict(value)


def estimated_cost(usage, price):
    price = validate_price(price)
    if (
        price is None
        or usage.input_tokens is None
        or usage.output_tokens is None
        or usage.source == "unknown"
    ):
        return None
    if usage.cached_tokens is None and Decimal(price["input"]) != Decimal(price["cached"]):
        return None  # Never invent a cache-hit split for a reseller/partial usage record.
    cache = usage.cached_tokens or 0
    cost = (
        (usage.input_tokens - cache) * Decimal(price["input"])
        + cache * Decimal(price["cached"])
        + usage.output_tokens * Decimal(price["output"])
    ) / Decimal(1000000)
    return str(cost.quantize(Decimal("0.000000000001")))
