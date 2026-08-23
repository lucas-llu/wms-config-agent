from core.settings import SplitterSettings
from core.types import Document
from ingestion.chunking import DocumentChunker
from libs.splitter import BaseSplitter


class FakeSplitter(BaseSplitter):
    def split_text(self, text: str, trace=None) -> list[str]:
        return ["Page one [IMAGE: diagram-1]", "Page two without an image"]


def test_document_chunker_adds_stable_ids_metadata_and_image_subset() -> None:
    text = "Page one [IMAGE: diagram-1]\n\nPage two without an image"
    first_page_end = text.index("\n\n")
    document = Document(
        id="doc-123",
        text=text,
        metadata={
            "source_path": "moca/policy.pdf",
            "version": "2024.1",
            "module": "policy",
            "pages": [
                {"page": 1, "start_offset": 0, "end_offset": first_page_end},
                {
                    "page": 2,
                    "start_offset": first_page_end + 2,
                    "end_offset": len(text),
                },
            ],
            "images": [
                {
                    "id": "diagram-1",
                    "path": "data/images/diagram-1.png",
                    "page": 1,
                    "text_offset": 9,
                    "text_length": 18,
                    "position": {},
                },
                {
                    "id": "unused-image",
                    "path": "data/images/unused.png",
                    "page": 2,
                    "text_offset": 0,
                    "text_length": 0,
                    "position": {},
                },
            ],
        },
    )
    settings = SplitterSettings(provider="recursive", chunk_size=100, chunk_overlap=10)
    chunker = DocumentChunker(settings, splitter=FakeSplitter())

    first_run = chunker.split_document(document)
    second_run = chunker.split_document(document)

    assert [chunk.id for chunk in first_run] == [chunk.id for chunk in second_run]
    assert first_run[0].id.startswith("doc-123_0000_")
    assert first_run[0].source_ref == "doc-123"
    assert first_run[0].metadata["chunk_index"] == 0
    assert first_run[0].metadata["version"] == "2024.1"
    assert first_run[0].metadata["image_refs"] == ["diagram-1"]
    assert [image["id"] for image in first_run[0].metadata["images"]] == ["diagram-1"]
    assert first_run[0].metadata["page_start"] == 1
    assert "images" not in first_run[1].metadata
    assert first_run[1].metadata["page_start"] == 2


def test_document_chunker_uses_configured_recursive_splitter() -> None:
    document = Document(
        id="doc-long",
        text=" ".join(f"setting-{index}" for index in range(40)),
        metadata={"source_path": "settings.md"},
    )
    settings = SplitterSettings(provider="recursive", chunk_size=80, chunk_overlap=10)

    chunks = DocumentChunker(settings).split_document(document)

    assert len(chunks) > 1
    assert all(chunk.source_ref == document.id for chunk in chunks)
    assert all(chunk.text in document.text for chunk in chunks)
