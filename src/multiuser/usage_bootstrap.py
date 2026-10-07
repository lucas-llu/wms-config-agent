"""Explicit P4 enablement; unknown reseller pricing defaults to unpriced."""

import hashlib
import json
import os

from multiuser.usage import UsageService


def metering_from_environment(settings, *, control=None):
    if os.getenv("WMS_P4_ENABLED") != "1":
        return None
    if os.getenv("WMS_P3_ENABLED") != "1" and os.getenv("WMS_P4_LIVE") != "1":
        raise ValueError("P4 requires the durable P3 run entry point")
    key = hashlib.sha256(f"{settings.llm.base_url}\0{settings.llm.model}".encode()).hexdigest()
    price = (
        json.loads(os.environ["WMS_MODEL_PRICE_JSON"])
        if os.getenv("WMS_MODEL_PRICE_JSON")
        else None
    )
    return UsageService(
        monthly_tokens=int(os.getenv("WMS_MONTHLY_TOKENS", "1000000")),
        run_budget=int(os.getenv("WMS_RUN_TOKEN_RESERVE", "100000")),
        model=settings.llm.model,
        provider_key=key,
        price=price,
        control=control,
    )
