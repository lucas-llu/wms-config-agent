"""Rule-based WMS query normalization, expansion, and metadata inference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_TOKEN = re.compile(r"[A-Za-z0-9_.$-]{2,}|[\u4e00-\u9fff]")
_PROCESS_CODE = re.compile(r"\bSWL\.[IOSV]\.\d{2}\.\d{2}\b", re.IGNORECASE)
_CONFIG_INTENT = re.compile(
    r"\b(configur(?:e|ed|ing|ation)|settings?|setup)\b|配置|设置",
    re.IGNORECASE,
)

_EXPANSIONS: tuple[tuple[str, str], ...] = (
    ("预约", "appointment schedule"),
    ("卡车", "truck transport equipment"),
    ("月台", "dock door"),
    ("上架", "putaway"),
    ("定向", "directed"),
    ("整托", "full pallet"),
    ("库位", "storage location"),
    ("收货", "receiving inbound"),
    ("波次", "wave"),
    ("补货", "replenishment"),
    ("巡回", "tour"),
    ("移动区域", "movement zone"),
    ("分配", "allocation allocate"),
    ("订单", "order"),
    ("移动库存", "inventory move"),
    ("库存移动", "inventory move"),
    ("库存调整", "inventory adjustment"),
    ("浏览器", "Web UI"),
    ("循环盘点", "cycle count"),
    ("盘点", "count inventory"),
    ("越库", "cross dock"),
    ("发货", "shipping outbound"),
    ("直接", "direct"),
    ("不经过存储", "without storage direct cross dock"),
    ("运输设备", "transport equipment"),
    ("安全检查", "safety check"),
    ("暂存区", "staging lane"),
    ("装载", "load"),
    ("工作单", "work order"),
    ("工作单创建", "work order creation"),
    ("增值服务", "value added service VAS"),
    ("配置", "configuration settings"),
    ("browser", "Web UI"),
    ("without storage", "direct cross dock"),
    ("storage location", "putaway location"),
    ("cycle count", "RF Based Cycle Count"),
    ("inventory adjustment", "RF Inventory Adjustment"),
    ("replenishment", "RF replenishment tour"),
)

_FILTER_ALIASES = {
    "collection": "collection",
    "domain": "domain",
    "module": "module",
    "document_type": "document_type",
    "doc_type": "document_type",
    "process_code": "process_code",
    "process_stage": "process_stage",
    "site": "site",
    "environment": "environment",
    "version": "version",
}

_GENERIC_TERMS = {
    "a",
    "an",
    "and",
    "can",
    "configuration",
    "configure",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "of",
    "on",
    "or",
    "settings",
    "setup",
    "the",
    "to",
    "with",
    "何",
    "如",
    "怎",
    "么",
    "配",
    "设",
    "置",
}


@dataclass(frozen=True, slots=True)
class ProcessedQuery:
    original_query: str
    normalized_query: str
    retrieval_query: str
    keywords: tuple[str, ...]
    filters: dict[str, str | int | float | bool]
    expansions: tuple[str, ...]
    specific_terms: tuple[str, ...]


class QueryProcessor:
    """Provide deterministic query features before retrieval."""

    def process(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> ProcessedQuery:
        normalized = " ".join(query.split())
        if not normalized:
            raise ValueError("query must be a non-empty string")

        resolved_filters = self._normalize_filters(filters or {})
        self._infer_filters(normalized, resolved_filters)
        expansions = self._expand(normalized)
        retrieval_query = " ".join((normalized, *expansions))
        keywords = tuple(dict.fromkeys(token.lower() for token in _TOKEN.findall(retrieval_query)))
        if not keywords:
            raise ValueError("query did not contain searchable terms")
        return ProcessedQuery(
            original_query=query,
            normalized_query=normalized,
            retrieval_query=retrieval_query,
            keywords=keywords,
            filters=resolved_filters,
            expansions=expansions,
            specific_terms=tuple(token for token in keywords if token not in _GENERIC_TERMS),
        )

    @staticmethod
    def _expand(query: str) -> tuple[str, ...]:
        lowered = query.lower()
        return tuple(expansion for phrase, expansion in _EXPANSIONS if phrase.lower() in lowered)

    @staticmethod
    def _normalize_filters(
        filters: dict[str, Any],
    ) -> dict[str, str | int | float | bool]:
        normalized: dict[str, str | int | float | bool] = {}
        for key, value in filters.items():
            canonical = _FILTER_ALIASES.get(key)
            if canonical is None:
                raise ValueError(f"Unsupported metadata filter: {key}")
            if not isinstance(value, str | int | float | bool):
                raise ValueError(f"Metadata filter {key} must be a scalar value")
            normalized[canonical] = value
        return normalized

    @staticmethod
    def _infer_filters(
        query: str,
        filters: dict[str, str | int | float | bool],
    ) -> None:
        if "document_type" not in filters and _CONFIG_INTENT.search(query):
            filters["document_type"] = "configuration"
        if "process_code" not in filters and (match := _PROCESS_CODE.search(query)):
            filters["process_code"] = match.group(0).upper()

        if "domain" in filters:
            return
        lowered = query.lower()
        domains = {"Inbound" for marker in ("inbound", "收货", "入库") if marker in lowered}
        domains.update("Outbound" for marker in ("outbound", "发货", "出库") if marker in lowered)
        if len(domains) == 1:
            filters["domain"] = domains.pop()


def build_store_filters(
    filters: dict[str, str | int | float | bool],
) -> dict[str, Any] | None:
    """Convert flat application filters to Chroma's where expression."""
    if not filters:
        return None
    if len(filters) == 1:
        return dict(filters)
    return {"$and": [{key: value} for key, value in sorted(filters.items())]}
