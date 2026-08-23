from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.settings import ImageCaptionerSettings, SplitterSettings, TransformSettings
from core.types import Document
from ingestion.corpus_manifest import CorpusManifestEntry
from ingestion.corpus_processor import CorpusProcessor
from ingestion.storage import ImageStorage
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.loader.base_loader import BaseLoader

pytestmark = pytest.mark.integration


class ImageDocumentLoader(BaseLoader):
    def load(self, path, metadata=None) -> Document:
        source = Path(path)
        image = source.parent / "diagram.png"
        image.write_bytes(b"diagram-bytes")
        text = "Page 1 of 1\nMOCA    receiving configuration\n[IMAGE: img-1]"
        values = dict(metadata or {})
        values.update(
            {
                "source_path": source.as_posix(),
                "file_hash": self.compute_file_hash(source),
                "images": [{"id": "img-1", "path": str(image), "page": 1}],
                "pages": [{"page": 1, "start_offset": 0, "end_offset": len(text)}],
            }
        )
        return Document(id="doc-1", text=text, metadata=values)


def test_corpus_processor_runs_transform_chain_and_indexes_images(tmp_path: Path) -> None:
    source = tmp_path / "SWL.I.01.01 Receiving Configuration.pdf"
    source.write_bytes(b"fake-pdf")
    entry = CorpusManifestEntry(
        schema_version=1,
        document_id="doc-1",
        file_hash=BaseLoader.compute_file_hash(source),
        source_path=source.name,
        source_name=source.name,
        title="Receiving Configuration",
        process_code="SWL.I.01.01",
        domain="Inbound",
        process_stage="I1.Pre-Receiving",
        document_type="configuration",
        page_count=1,
        size_bytes=source.stat().st_size,
        modified_at=datetime.now(UTC).isoformat(),
        version="test",
    )
    image_storage = ImageStorage(
        tmp_path / "data" / "images", tmp_path / "data" / "image_index.db"
    )
    rule_settings = TransformSettings(enabled=True, use_llm=False)
    caption_settings = ImageCaptionerSettings(
        enabled=False,
        prompt_path=tmp_path / "missing.txt",
        append_to_text=True,
    )
    processor = CorpusProcessor(
        source_root=tmp_path,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "history.db",
        splitter_settings=SplitterSettings(
            provider="recursive", chunk_size=500, chunk_overlap=50
        ),
        extract_images=True,
        transforms=(
            ChunkRefiner(rule_settings),
            MetadataEnricher(rule_settings),
            ImageCaptioner(caption_settings),
        ),
        image_storage=image_storage,
        image_collection="manuals",
        loader_builder=lambda *args, **kwargs: ImageDocumentLoader(),
    )

    report = processor.process([entry])

    assert report.succeeded == 1
    payload = json.loads(
        (tmp_path / "processed" / "chunks" / "doc-1.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert "Page 1 of 1" not in payload["text"]
    assert "MOCA receiving configuration" in payload["text"]
    assert payload["metadata"]["title"] == "Receiving Configuration"
    assert payload["metadata"]["summary"]
    assert payload["metadata"]["image_caption_status"] == "disabled"
    stored_path = Path(payload["metadata"]["images"][0]["path"])
    assert stored_path.is_file()
    assert stored_path.parent.name == "manuals"
    assert image_storage.get_path("img-1") == stored_path
