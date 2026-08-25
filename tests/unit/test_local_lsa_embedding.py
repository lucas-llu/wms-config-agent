from __future__ import annotations

import pytest

from libs.embedding import LocalLSAEmbedding

TEXTS = [
    "RF directed putaway configuration controls inventory movement",
    "Outbound staging lane assignment uses dock door planning",
    "Appointment creation configures inbound transport schedules",
    "MOCA policy values determine warehouse execution behavior",
]


def test_local_lsa_fits_persists_and_reloads(tmp_path) -> None:
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)

    assert embedding.fit(TEXTS) is True
    vectors = embedding.embed(TEXTS)
    signature = embedding.signature

    assert len(vectors) == len(TEXTS)
    assert {len(vector) for vector in vectors} == {3}
    assert embedding.model_path.is_file()

    reloaded = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)
    assert reloaded.fit(TEXTS) is False
    assert reloaded.signature == signature
    assert reloaded.embed_query("putaway settings") == pytest.approx(
        embedding.embed_query("putaway settings")
    )


def test_local_lsa_rejects_empty_corpus(tmp_path) -> None:
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)

    with pytest.raises(ValueError, match="must not be empty"):
        embedding.fit([])


def test_prepared_fit_can_rollback_without_replacing_query_model(tmp_path) -> None:
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)
    embedding.fit(TEXTS)
    original_signature = embedding.signature
    original_query = embedding.embed_query("putaway settings")
    original_cache = embedding.model_path.read_bytes()

    changed = embedding.prepare_fit(
        [
            "cycle count adjustment policy",
            "inventory hold release configuration",
        ]
    )

    assert changed is True
    assert embedding.actual_dimensions == 1
    assert embedding.model_path.read_bytes() == original_cache

    embedding.rollback_fit()

    assert embedding.signature == original_signature
    assert embedding.embed_query("putaway settings") == pytest.approx(original_query)
    assert embedding.model_path.read_bytes() == original_cache
