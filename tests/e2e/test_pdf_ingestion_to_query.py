from __future__ import annotations

from pathlib import Path

import pytest

from core.query_engine import (
    DenseRetriever,
    HybridSearch,
    QueryProcessor,
    ReciprocalRankFusion,
    SparseRetriever,
)
from core.settings import (
    ImageCaptionerSettings,
    RetrievalSettings,
    SplitterSettings,
    TransformSettings,
)
from ingestion import (
    CorpusManifestBuilder,
    CorpusProcessor,
    IndexingPipeline,
    load_preprocessed_chunks,
)
from ingestion.storage import BM25Indexer, ImageStorage
from ingestion.transform import ChunkRefiner, ImageCaptioner, MetadataEnricher
from libs.embedding import LocalLSAEmbedding
from libs.vector_store import ChromaStore

pytestmark = pytest.mark.e2e


def _write_text_and_image_pdf(path: Path, text: str) -> None:
    content_stream = (
        f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET q 20 0 0 20 72 650 cm /Im0 Do Q"
    ).encode("ascii")
    image_stream = b"FF0000>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Im0 6 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content_stream)).encode()
        + b" >>\nstream\n"
        + content_stream
        + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /ASCIIHexDecode /Length "
        + str(len(image_stream)).encode()
        + b" >>\nstream\n"
        + image_stream
        + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode())
        payload.extend(value)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(payload)


def test_real_pdf_image_transform_index_and_query(tmp_path: Path) -> None:
    source_root = tmp_path / "corpus" / "Inbound" / "I9.Demo"
    source_root.mkdir(parents=True)
    _write_text_and_image_pdf(
        source_root / "SWL.I.99.01 Demo Alpha Configuration.pdf",
        "SWL.I.99.01 alpha receiving policy configuration and dock settings",
    )
    _write_text_and_image_pdf(
        source_root / "SWL.I.99.02 Demo Beta Configuration.pdf",
        "SWL.I.99.02 beta cycle count tolerance configuration",
    )
    corpus_root = tmp_path / "corpus"
    entries = CorpusManifestBuilder().scan(corpus_root)
    image_storage = ImageStorage(
        tmp_path / "data" / "images",
        tmp_path / "data" / "image_index.db",
    )
    rule_settings = TransformSettings(enabled=True, use_llm=False)
    report = CorpusProcessor(
        source_root=corpus_root,
        output_root=tmp_path / "processed",
        database_path=tmp_path / "history.db",
        splitter_settings=SplitterSettings(provider="recursive", chunk_size=500, chunk_overlap=50),
        extract_images=True,
        transforms=(
            ChunkRefiner(rule_settings),
            MetadataEnricher(rule_settings),
            ImageCaptioner(
                ImageCaptionerSettings(
                    enabled=False,
                    prompt_path=tmp_path / "missing-prompt.txt",
                    append_to_text=True,
                )
            ),
        ),
        image_storage=image_storage,
        image_collection="e2e-manuals",
    ).process(entries)
    chunks = load_preprocessed_chunks(tmp_path / "processed" / "chunks")
    embedding = LocalLSAEmbedding(dimensions=2, cache_dir=tmp_path / "models")
    vector_store = ChromaStore(
        persist_path=tmp_path / "chroma",
        collection_name="e2e_chunks",
    )
    bm25 = BM25Indexer(tmp_path / "bm25")
    IndexingPipeline(
        embedding=embedding,
        vector_store=vector_store,
        bm25_indexer=bm25,
        batch_size=2,
    ).index(chunks, force=True)
    retrieval = RetrievalSettings(
        sparse_backend="bm25",
        fusion_algorithm="rrf",
        top_k_dense=4,
        top_k_sparse=4,
        top_k_final=2,
        rrf_k=60,
        max_chunks_per_document=2,
        min_fused_score=0.02,
    )
    results = HybridSearch(
        retrieval,
        QueryProcessor(),
        DenseRetriever(embedding, vector_store),
        SparseRetriever(bm25, vector_store),
        ReciprocalRankFusion(retrieval.rrf_k),
    ).search("SWL.I.99.01 alpha receiving configuration", top_k=2)

    assert report.succeeded == 2 and report.failed == 0
    assert len(chunks) == 2
    assert vector_store.count() == bm25.count() == 2
    assert image_storage.count() == 2
    assert results[0].metadata["process_code"] == "SWL.I.99.01"
    assert results[0].metadata["image_refs"]
    assert Path(results[0].metadata["images"][0]["path"]).is_file()
