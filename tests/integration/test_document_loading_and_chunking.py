from pathlib import Path

import pytest

from core.settings import SplitterSettings
from ingestion.chunking import DocumentChunker
from libs.loader import LoaderFactory


@pytest.mark.integration
def test_authorized_markdown_becomes_traceable_chunks() -> None:
    source = Path("tests/fixtures/sample_documents/sample_moca_config.md")
    loader = LoaderFactory.create(source)
    document = loader.load(source, {"collection": "wms-fixtures"})
    chunker = DocumentChunker(
        SplitterSettings(provider="recursive", chunk_size=120, chunk_overlap=20)
    )

    chunks = chunker.split_document(document)

    assert len(chunks) >= 2
    assert all(chunk.source_ref == document.id for chunk in chunks)
    assert all(chunk.metadata["source_path"].endswith(source.as_posix()) for chunk in chunks)
    assert all(chunk.metadata["module"] == "inventory" for chunk in chunks)
    assert all(chunk.metadata["version"] == "test-fixture" for chunk in chunks)
    assert len({chunk.id for chunk in chunks}) == len(chunks)
