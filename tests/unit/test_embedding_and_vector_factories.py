from __future__ import annotations

import pytest

from core.settings import EmbeddingSettings, VectorStoreSettings
from libs.embedding import EmbeddingFactory, LocalLSAEmbedding
from libs.vector_store import ChromaStore, VectorStoreFactory


def test_embedding_factory_creates_local_lsa(tmp_path) -> None:
    settings = EmbeddingSettings(
        provider="local_lsa",
        model="test-lsa",
        dimensions=3,
        batch_size=2,
        cache_dir=tmp_path,
    )

    assert isinstance(EmbeddingFactory.create(settings), LocalLSAEmbedding)


def test_vector_store_factory_creates_chroma(tmp_path) -> None:
    settings = VectorStoreSettings(
        backend="chroma",
        persist_path=tmp_path,
        collection_name="factory_test",
    )

    assert isinstance(VectorStoreFactory.create(settings), ChromaStore)


def test_factories_reject_unknown_providers(tmp_path) -> None:
    embedding = EmbeddingSettings(
        provider="unknown",
        model="none",
        dimensions=3,
        batch_size=2,
        cache_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="Unknown embedding provider"):
        EmbeddingFactory.create(embedding)
