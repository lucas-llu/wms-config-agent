"""Bounded, privacy-safe supplier smoke/pressure measurements, not platform SLO."""

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from multiuser.metering import normalize_usage

PROMPT = [{"role": "user", "content": "This is a synthetic load probe. Reply with OK only."}]
# Conservative byte ceiling plus framing reserve; never send customer knowledge or prompts.
INPUT_RESERVE = 512


def percentile(values, fraction=0.95):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


@dataclass(frozen=True)
class ProbeBudget:
    approved: bool
    max_calls: int
    concurrency: int
    rpm: int
    tpm: int
    max_output_tokens: int
    total_token_budget: int

    def __post_init__(self):
        if self.approved is not True:
            raise PermissionError("Human supplier budget approval required")
        bounds = {
            "max_calls": (1, 100),
            "concurrency": (1, 20),
            "rpm": (1, 10000),
            "tpm": (1, 10000000),
            "max_output_tokens": (1, 64000),
            "total_token_budget": (1, 10000000),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid supplier budget")
        allocation = INPUT_RESERVE + self.max_output_tokens
        if allocation > min(self.tpm, self.total_token_budget):
            raise ValueError("One request exceeds approved token allowance")


def probe_supplier(model, budget, *, clock=time.monotonic):
    if not isinstance(budget, ProbeBudget):
        raise TypeError("Validated human-approved budget required")
    if model.max_retries != 0 or model.max_tokens != budget.max_output_tokens:
        raise ValueError("One-attempt transport and exact approved output cap required")
    condition = threading.Condition()
    window = []
    remaining = budget.total_token_budget
    attempted, rows = 0, []
    allocation = INPUT_RESERVE + budget.max_output_tokens
    started = clock()
    stop = False

    def one(_):
        nonlocal remaining, attempted, stop
        with condition:
            while True:
                now = clock()
                window[:] = [stamp for stamp in window if now - stamp < 60]
                if stop or attempted >= budget.max_calls or remaining < allocation:
                    return
                if len(window) < budget.rpm and (len(window) + 1) * allocation <= budget.tpm:
                    window.append(now)
                    remaining -= allocation
                    attempted += 1
                    break
                # Do not wait forever; a fresh operator-approved batch can continue later.
                if now - started > 120:
                    stop = True
                    return
                condition.wait(timeout=0.1)
        call_start = clock()
        try:
            response = model.chat(PROMPT)
            usage = normalize_usage(response.metadata.get("usage"))
            if usage.source != "provider" or usage.total_tokens is None:
                raise ValueError("Unverified supplier usage; keep reservation")
            result = {
                "status": "returned",
                "seconds": round(clock() - call_start, 4),
                "usage_source": usage.source,
                "total_tokens": usage.total_tokens,
            }
            with condition:
                # Unknown calls consume their entire reservation. Never silently retry.
                if usage.total_tokens is not None:
                    remaining += allocation - usage.total_tokens
                    if usage.total_tokens > allocation:
                        stop = True
                        result["budget_breach"] = True
        except Exception as exc:
            # No raw error/body/headers (they may contain credentials or response text).
            result = {
                "status": "unknown",
                "seconds": round(clock() - call_start, 4),
                "error_type": type(exc).__name__,
                "total_tokens": None,
            }
            with condition:
                stop = True
        with condition:
            rows.append(result)
            condition.notify_all()

    with ThreadPoolExecutor(max_workers=budget.concurrency) as pool:
        list(pool.map(one, range(budget.max_calls)))
    returned = [r for r in rows if r["status"] == "returned"]
    return {
        "scope": "supplier_only_synthetic_prompt_not_platform_slo",
        "attempted": attempted,
        "returned": len(returned),
        "unknown": len(rows) - len(returned),
        "budget_remaining": max(0, remaining),
        "p95_supplier_seconds": percentile([r["seconds"] for r in returned]),
        "rows": rows,
        "retries": 0,
        "concurrency_cap": budget.concurrency,
        "complete": len(returned) == budget.max_calls and not stop,
    }
