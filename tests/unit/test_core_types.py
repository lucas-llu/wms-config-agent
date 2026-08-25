import json

import pytest

from core.types import Chunk, ChunkRecord, ContractError, Document


def test_contracts_have_stable_serialization() -> None:
    metadata = {
        "source_path": "docs/moca/commands.md",
        "module": "inventory",
        "images": [],
    }
    document = Document(id="doc-1", text="MOCA configuration", metadata=metadata)
    chunk = Chunk(
        id="chunk-1",
        text="configuration",
        metadata=metadata,
        start_offset=5,
        end_offset=18,
        source_ref="section-2",
    )
    record = ChunkRecord(
        id=chunk.id,
        text=chunk.text,
        metadata=chunk.metadata,
        dense_vector=[0.1, 0.2],
        sparse_vector={"configuration": 1.0},
    )

    assert document.to_dict()["metadata"]["source_path"] == "docs/moca/commands.md"
    assert chunk.to_dict()["source_ref"] == "section-2"
    assert json.loads(record.to_json())["dense_vector"] == [0.1, 0.2]


def test_source_path_is_required() -> None:
    with pytest.raises(ContractError, match=r"metadata\.source_path"):
        Document(id="doc-1", text="content", metadata={})


def test_chunk_offsets_must_be_ordered() -> None:
    with pytest.raises(ContractError, match="end_offset"):
        Chunk(
            id="chunk-1",
            text="content",
            metadata={"source_path": "source.md"},
            start_offset=10,
            end_offset=4,
        )
