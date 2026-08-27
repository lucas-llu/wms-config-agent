from __future__ import annotations

import warnings

import pytest

from libs.embedding import LocalLSAEmbedding
from libs.embedding import local_lsa_embedding as local_lsa_module

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


def test_local_lsa_load_handles_joblib_numpy_25_shape_deprecation(tmp_path) -> None:
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)
    embedding.fit(TEXTS)
    reloaded = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "error",
            message=r"Setting the shape on a NumPy array has been deprecated in NumPy 2\.5\.",
            category=DeprecationWarning,
        )
        assert reloaded.signature == embedding.signature


def test_local_lsa_load_does_not_hide_unrelated_deprecations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedding = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)
    embedding.fit(TEXTS)
    original_load = local_lsa_module.joblib.load

    def noisy_load(path):
        warnings.warn("unrelated deprecation", DeprecationWarning, stacklevel=1)
        return original_load(path)

    monkeypatch.setattr(local_lsa_module.joblib, "load", noisy_load)
    reloaded = LocalLSAEmbedding(dimensions=3, cache_dir=tmp_path)

    with (
        pytest.raises(DeprecationWarning, match="unrelated deprecation"),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("error", DeprecationWarning)
        _ = reloaded.signature
