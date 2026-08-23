# Benchmark V1

Benchmark V1 freezes retrieval quality before the Day 6 evaluation framework and later
changes to chunking, embeddings, fusion, and reranking. A benchmark gate is a regression
floor, not the target quality level.

## Dataset split and privacy

Two datasets serve different purposes:

- `tests/fixtures/golden_test_set.json` contains four sanitized process-code smoke cases and
  is safe to commit.
- `data/evaluation/benchmark_v1.private.json` contains the 40-case real-corpus benchmark.
  The entire `data/` directory is ignored and must remain local.

Full reports are also written below `data/evaluation/`. They contain queries and relative
document references and must not be committed. The report intentionally excludes retrieved
text and absolute filesystem paths.

## Ground-truth policy

Every case has a stable ID, one intent category, a query, optional explicit metadata filters,
and an `expected` object. Positive relevance can be constrained by process code, sanitized
relative source, domain, and document type; all specified constraints must match the same
retrieval result. Negative cases set `should_refuse` and cannot contain positive labels.

Labels follow these rules:

1. An authorized reviewer verifies the expected document against the local source corpus.
2. Ambiguous questions list every acceptable process code or source instead of forcing one.
3. Configuration and operation documents are distinct relevance targets.
4. A refusal case is included only after confirming that the authorized corpus has no answer.
5. Generated or paraphrased questions require human review before entering the frozen set.

The loader rejects duplicate IDs, empty positive labels, unknown fields, unsupported filters,
absolute source paths, and parent-directory traversal.

## V1 composition

| Category | Cases | Purpose |
|---|---:|---|
| Exact process code | 12 | Identifier lookup and metadata filtering |
| Bilingual semantic | 16 | Chinese/English business-language retrieval |
| Document type filter | 8 | Configuration versus operation separation |
| Unsupported refusal | 4 | Evidence-based refusal behavior |
| Total | 40 | Local private benchmark |

## Metrics

- `Hit@1/3/5`: fraction of positive cases with a relevant result in the first K results.
- `MRR@5`: mean reciprocal rank of the first relevant result up to rank five.
- `refusal_accuracy`: fraction of negative cases marked as insufficient evidence.
- `evidence_accuracy`: positive cases require a relevant result and sufficient evidence;
  negative cases require insufficient evidence.
- `P50/P95 latency`: end-to-end local retrieval latency per case.

## Frozen Baseline V1

The baseline was captured from commit `a26da30` against 1,274 indexed chunks:

| Metric | Baseline | Regression floor |
|---|---:|---:|
| Hit@1 | 72.22% | 70% |
| Hit@3 | 86.11% | 85% |
| Hit@5 | 88.89% | 88% |
| MRR@5 | 78.94% | 78% |
| Refusal accuracy | 100% | 75% |
| Evidence accuracy | 90% | 88% |
| P95 latency | 76.51 ms | 2,000 ms |

Exact process-code retrieval and unsupported refusal both scored 100%. The semantic category
has four known misses; these remain in the dataset as Day 6 improvement targets.

Latency depends on local hardware and a warm model/index cache. The generous latency gate is
intended to catch severe regressions, not compare different machines.

## Run the benchmark

Run the private benchmark and enforce all regression floors:

```powershell
.\.venv\Scripts\python.exe scripts\run_benchmark.py `
  --dataset data\evaluation\benchmark_v1.private.json `
  --output data\evaluation\baseline_v1.json `
  --enforce-thresholds
```

Run the committed smoke set:

```powershell
.\.venv\Scripts\python.exe scripts\run_benchmark.py `
  --dataset tests\fixtures\golden_test_set.json `
  --output data\evaluation\public_smoke_baseline.json `
  --enforce-thresholds
```

The command exits with status 1 only when `--enforce-thresholds` is supplied and at least one
configured gate fails. Each report records its dataset SHA-256 fingerprint, Git revision,
retrieval providers, aggregate/category metrics, failed cases, and sanitized ranked results.

## Compare a candidate with Baseline V1

```powershell
.\.venv\Scripts\python.exe scripts\run_benchmark.py `
  --dataset data\evaluation\benchmark_v1.private.json `
  --baseline data\evaluation\baseline_v1.json `
  --output data\evaluation\candidate.json `
  --enforce-thresholds `
  --fail-on-regression
```

Comparison is rejected when dataset fingerprints or case IDs differ. Quality regression is
reported separately from per-case fixes and failures. Dense, BM25, raw fusion, and final
relevant ranks are retained for diagnosing whether a miss came from recall or final ranking.

## Day 6 candidate result

After bilingual domain expansion, broader configuration-intent detection, and deterministic
trusted-title metadata boosting, the same 40-case dataset produced:

| Metric | Baseline V1 | Day 6 candidate | Delta |
|---|---:|---:|---:|
| Hit@1 | 72.22% | 100% | +27.78 pp |
| Hit@3 | 86.11% | 100% | +13.89 pp |
| Hit@5 | 88.89% | 100% | +11.11 pp |
| MRR@5 | 78.94% | 100% | +21.06 pp |
| Refusal accuracy | 100% | 100% | 0 pp |
| Evidence accuracy | 90% | 100% | +10 pp |

All four known failures were fixed and no new failed case was introduced. These numbers
describe this frozen dataset only; new human-reviewed holdout cases must be added as WMS
coverage expands to reduce overfitting risk.
