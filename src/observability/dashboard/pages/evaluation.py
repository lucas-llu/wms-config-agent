"""Benchmark execution, metrics, comparison, and privacy-safe history."""

from __future__ import annotations

from typing import Any

import streamlit as st

from observability.dashboard.services import EvaluationService, get_evaluation_service


def render(evaluation_service: EvaluationService | None = None) -> None:
    st.title("Evaluation")
    st.caption("Run approved sanitized benchmarks and compare privacy-safe history")
    try:
        service = evaluation_service or get_evaluation_service()
        datasets = service.list_datasets()
        history = service.list_reports()
    except Exception as exc:
        st.error(f"Evaluation services are unavailable: {type(exc).__name__}: {exc}")
        st.info("Check the benchmark dataset and settings, then reload this page.")
        return

    if not datasets:
        st.info("No approved benchmark datasets are configured.")
        return
    labels = {
        item.identifier: f"{item.name} · {item.case_count} cases · {item.fingerprint[:12]}"
        for item in datasets
    }
    selected_id = st.selectbox("Dataset", list(labels), format_func=labels.get)
    selected = next(item for item in datasets if item.identifier == selected_id)
    st.caption(selected.description)

    compatible = [
        report for report in history if report.dataset_fingerprint == selected.fingerprint
    ]
    baseline_labels = {"": "No baseline"}
    baseline_labels.update(
        {
            report.identifier: (
                f"{report.created_at} · {'PASS' if report.passed else 'FAIL'} · {report.identifier}"
            )
            for report in compatible
        }
    )
    baseline_id = st.selectbox(
        "Baseline",
        list(baseline_labels),
        format_func=baseline_labels.get,
    )
    if st.button("Run evaluation", type="primary"):
        try:
            result = service.run(selected_id, baseline_identifier=baseline_id or None)
        except Exception as exc:
            st.error(f"Evaluation failed: {type(exc).__name__}: {exc}")
        else:
            st.session_state["evaluation_result"] = result
            st.success(
                f"Benchmark {'passed' if result.report.passed else 'failed'}; "
                f"saved privacy-safe report {result.report_summary.identifier}."
            )

    result = st.session_state.get("evaluation_result")
    if result is not None and result.report.dataset_fingerprint == selected.fingerprint:
        _render_result(result)
    _render_history(history)


def _render_result(result: Any) -> None:
    report = result.report
    metrics = report.metrics
    st.subheader("Latest result")
    columns = st.columns(7)
    values = (
        ("Hit@1", _percent(metrics.get("hit_at_1"))),
        ("Hit@3", _percent(metrics.get("hit_at_3"))),
        ("Hit@5", _percent(metrics.get("hit_at_5"))),
        ("MRR@5", _percent(metrics.get("mrr_at_5"))),
        ("Refusal", _percent(metrics.get("refusal_accuracy"))),
        ("Evidence", _percent(metrics.get("evidence_accuracy"))),
        ("P95 latency", _milliseconds(metrics.get("p95_latency_ms"))),
    )
    for column, (label, value) in zip(columns, values, strict=True):
        column.metric(label, value)

    threshold_rows = [
        {
            "Threshold": name,
            "Target": report.thresholds.get(name),
            "Passed": passed,
        }
        for name, passed in sorted(report.threshold_results.items())
    ]
    if threshold_rows:
        st.subheader("Thresholds")
        st.dataframe(threshold_rows, width="stretch", hide_index=True)
    if report.category_metrics:
        st.subheader("Categories")
        st.dataframe(
            [{"Category": name, **metrics} for name, metrics in report.category_metrics.items()],
            width="stretch",
            hide_index=True,
        )
    failed = [case.case_id for case in report.cases if not case.passed]
    if failed:
        st.warning("Failed case IDs: " + ", ".join(failed))

    comparison = result.comparison
    if comparison is not None:
        st.subheader("Baseline comparison")
        rows = [
            {"Metric": name, "Delta": delta}
            for name, delta in sorted(comparison.metric_deltas.items())
        ]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
        if comparison.regressions or comparison.new_failures:
            st.error(
                "Regression detected: "
                + ", ".join((*comparison.regressions, *comparison.new_failures))
            )
        else:
            st.success("No quality regression against the selected baseline.")


def _render_history(history: tuple[Any, ...]) -> None:
    st.subheader("Privacy-safe history")
    if not history:
        st.info("No Dashboard benchmark history is available yet.")
        return
    st.dataframe(
        [
            {
                "Created": item.created_at,
                "Dataset": item.dataset_name,
                "Cases": item.case_count,
                "Hit@3": item.metrics.get("hit_at_3"),
                "MRR@5": item.metrics.get("mrr_at_5"),
                "Refusal": item.metrics.get("refusal_accuracy"),
                "Evidence": item.metrics.get("evidence_accuracy"),
                "P95 ms": item.metrics.get("p95_latency_ms"),
                "Passed": item.passed,
                "Failed case IDs": ", ".join(item.failed_cases),
            }
            for item in history
        ],
        width="stretch",
        hide_index=True,
    )


def _percent(value: object) -> str:
    return "N/A" if value is None else f"{float(value) * 100:.1f}%"


def _milliseconds(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.1f} ms"


if __name__ == "__main__":
    render()
