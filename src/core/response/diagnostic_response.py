"""Deterministic troubleshooting presentation; never infer a root cause from retrieval."""

from __future__ import annotations

import re
from typing import Any

from core.response.response_builder import EvidenceResponse

_SECTIONS = (
    ("causes", "可能原因", r"\bcause\b|\bbecause\b|原因|由于"),
    ("role", "角色与权限", r"\brole\b|\bpermission\w*\b|角色|权限"),
    ("equipment", "车辆与设备", r"\bequipment\b|\bvehicle\b|设备|车辆"),
    ("rules", "作业规则", r"\brule\w*\b|\bpolicy\b|规则|策略"),
    ("environment", "环境差异", r"\benvironment\b|\bproduction\b|\btest\b|环境|生产"),
    ("verification", "验证证据", r"\bverif\w*\b|\bcheck\b|验证|检查"),
)


def diagnostic_payload(response: EvidenceResponse) -> dict[str, Any]:
    """Group exact excerpts, not generated advice; keyword matches are routing only."""
    citations = response.citations if response.status == "evidence_found" else ()
    sections = []
    for key, title, pattern in _SECTIONS:
        evidence = [
            {"text": citation.excerpt, "citation_ids": [citation.index]}
            for citation in citations
            if citation.excerpt.strip() and re.search(pattern, citation.excerpt, re.IGNORECASE)
        ]
        sections.append(
            {
                "key": key,
                "title": title,
                "status": "document_evidence" if evidence else "evidence_gap",
                "evidence": evidence,
                "gap": "" if evidence else "未检索到该维度的明确证据，不能据此建议配置变更。",
            }
        )
    conclusion = "尚未确认故障原因；文档片段不代表当前 WMS 的实际配置或运行状态。"
    lines = ["## 排查结论", conclusion, "", "以下为文档原文，不是可直接执行的指令。"]
    for section in sections:
        lines.extend(["", f"## {section['title']}"])
        if not section["evidence"]:
            lines.append(section["gap"])
        for item in section["evidence"]:
            lines.append(f"> [{item['citation_ids'][0]}] {item['text']}")
    lines.extend(["", "## 引用", response.markdown])
    payload = response.to_dict()
    payload["markdown"] = "\n".join(lines)
    payload["troubleshooting"] = {
        "schema_version": 1,
        "conclusion": conclusion,
        "root_cause_confirmed": False,
        "sections": sections,
    }
    return payload


def diagnostic_schema() -> dict[str, Any]:
    def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    evidence = object_schema(
        {
            "text": {"type": "string", "minLength": 1},
            "citation_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "integer", "minimum": 1},
            },
        }
    )
    section = object_schema(
        {
            "key": {"type": "string", "enum": [item[0] for item in _SECTIONS]},
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["document_evidence", "evidence_gap"]},
            "evidence": {"type": "array", "items": evidence},
            "gap": {"type": "string"},
        }
    )
    return object_schema(
        {
            "schema_version": {"type": "integer", "const": 1},
            "conclusion": {"type": "string"},
            "root_cause_confirmed": {"type": "boolean", "const": False},
            "sections": {"type": "array", "minItems": 6, "maxItems": 6, "items": section},
        }
    )
