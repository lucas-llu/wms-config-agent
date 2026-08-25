"""Document ingestion pipeline."""

from ingestion.corpus_manifest import (
    CorpusManifestBuilder,
    CorpusManifestEntry,
    CorpusManifestSummary,
)
from ingestion.corpus_processor import CorpusProcessingReport, CorpusProcessor
from ingestion.document_manager import (
    CollectionStats,
    DeleteResult,
    DocumentDetail,
    DocumentInfo,
    DocumentManager,
)
from ingestion.factory import create_ingestion_pipeline
from ingestion.llm_failure_ledger import LLMFailureLedger, LLMFallback, collect_llm_fallbacks
from ingestion.pipeline import (
    IndexingPipeline,
    IndexingReport,
    IngestionPipeline,
    IngestionResult,
    load_preprocessed_chunks,
)

__all__ = [
    "CorpusManifestBuilder",
    "CorpusManifestEntry",
    "CorpusManifestSummary",
    "CorpusProcessingReport",
    "CorpusProcessor",
    "CollectionStats",
    "DeleteResult",
    "DocumentDetail",
    "DocumentInfo",
    "DocumentManager",
    "create_ingestion_pipeline",
    "IndexingPipeline",
    "IndexingReport",
    "IngestionPipeline",
    "IngestionResult",
    "LLMFailureLedger",
    "LLMFallback",
    "collect_llm_fallbacks",
    "load_preprocessed_chunks",
]
