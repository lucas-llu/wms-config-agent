"""Corpus-trained local TF-IDF + LSA dense embedding provider."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import joblib
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.preprocessing import Normalizer

from libs.embedding.base_embedding import BaseEmbedding


class LocalLSAEmbedding(BaseEmbedding):
    """Learn deterministic latent topic vectors without a native model runtime."""

    def __init__(
        self,
        *,
        model_name: str = "tfidf-svd",
        dimensions: int = 256,
        cache_dir: str | Path = "data/models/local_lsa",
        batch_size: int = 32,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than 0")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than 0")
        self.model_name = model_name
        self.dimensions = dimensions
        self.cache_dir = Path(cache_dir)
        self.batch_size = batch_size
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_name)
        self.model_path = self.cache_dir / f"{safe_name}-{dimensions}.joblib"
        self._features: FeatureUnion | None = None
        self._svd: TruncatedSVD | None = None
        self._normalizer: Normalizer | None = None
        self._corpus_hash: str | None = None
        self.actual_dimensions: int | None = None
        self._prepared_backup: (
            tuple[
                FeatureUnion | None,
                TruncatedSVD | None,
                Normalizer | None,
                str | None,
                int | None,
            ]
            | None
        ) = None
        self._prepared_model_bytes: bytes | None = None
        self._prepared_model_existed = False

    def fit(self, texts: list[str], *, force: bool = False) -> bool:
        """Fit and persist immediately for the standalone embedding contract."""

        changed = self.prepare_fit(texts, force=force)
        self.commit_fit()
        self.finalize_fit()
        return changed

    def prepare_fit(self, texts: list[str], *, force: bool = False) -> bool:
        """Fit in memory while leaving the last query-compatible model on disk.

        The ingestion coordinator commits this prepared model only after its dense and
        sparse indexes have both switched successfully.  On failure, ``rollback_fit``
        restores the in-memory provider and the previous cache remains untouched.
        """

        if self._prepared_backup is not None:
            raise RuntimeError("A Local LSA fit is already prepared")
        clean_texts = self._validate_texts(texts)
        corpus_hash = self._hash_corpus(clean_texts)
        if not force and self.model_path.is_file():
            self._load()
            if self._corpus_hash == corpus_hash:
                return False

        features = self._build_features()
        matrix = features.fit_transform(clean_texts)
        max_dimensions = min(matrix.shape[0] - 1, matrix.shape[1] - 1)
        if max_dimensions < 1:
            raise ValueError("At least two varied texts are required to fit local LSA embeddings")
        actual_dimensions = min(self.dimensions, max_dimensions)
        svd = TruncatedSVD(n_components=actual_dimensions, random_state=42)
        dense = svd.fit_transform(matrix)
        normalizer = Normalizer(copy=False)
        normalizer.fit(dense)

        self._prepared_backup = (
            self._features,
            self._svd,
            self._normalizer,
            self._corpus_hash,
            self.actual_dimensions,
        )
        self._prepared_model_existed = self.model_path.is_file()
        self._prepared_model_bytes = (
            self.model_path.read_bytes() if self._prepared_model_existed else None
        )
        self._features = features
        self._svd = svd
        self._normalizer = normalizer
        self._corpus_hash = corpus_hash
        self.actual_dimensions = actual_dimensions
        return True

    def commit_fit(self) -> None:
        """Atomically persist a prepared model after index synchronization succeeds."""

        if self._prepared_backup is None:
            return
        self._save()

    def finalize_fit(self) -> None:
        """Release rollback state after every coordinated storage commit succeeds."""

        self._prepared_backup = None
        self._prepared_model_bytes = None
        self._prepared_model_existed = False

    def rollback_fit(self) -> None:
        """Discard an uncommitted model and restore the prior in-memory provider."""

        if self._prepared_backup is None:
            return
        (
            self._features,
            self._svd,
            self._normalizer,
            self._corpus_hash,
            self.actual_dimensions,
        ) = self._prepared_backup
        if self._prepared_model_existed:
            assert self._prepared_model_bytes is not None
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.model_path.with_suffix(self.model_path.suffix + ".rollback.tmp")
            temporary.write_bytes(self._prepared_model_bytes)
            temporary.replace(self.model_path)
        else:
            self.model_path.unlink(missing_ok=True)
        self._prepared_backup = None
        self._prepared_model_bytes = None
        self._prepared_model_existed = False

    def embed(self, texts: list[str], trace: Any | None = None) -> list[list[float]]:
        del trace
        if not texts:
            return []
        clean_texts = self._validate_texts(texts)
        self._ensure_loaded()
        assert self._features is not None
        assert self._svd is not None
        assert self._normalizer is not None
        matrix = self._features.transform(clean_texts)
        dense = self._svd.transform(matrix)
        normalized = self._normalizer.transform(dense)
        return normalized.astype(float).tolist()

    @property
    def signature(self) -> str:
        self._ensure_loaded()
        payload = f"local_lsa:{self.model_name}:{self.actual_dimensions}:{self._corpus_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_features() -> FeatureUnion:
        return FeatureUnion(
            [
                (
                    "word",
                    TfidfVectorizer(
                        lowercase=True,
                        ngram_range=(1, 2),
                        min_df=1,
                        max_df=0.98,
                        sublinear_tf=True,
                        token_pattern=r"(?u)\b[\w.$-]{2,}\b",
                    ),
                ),
                (
                    "character",
                    TfidfVectorizer(
                        analyzer="char_wb",
                        ngram_range=(3, 5),
                        min_df=2,
                        max_features=50_000,
                        sublinear_tf=True,
                    ),
                ),
            ]
        )

    @staticmethod
    def _validate_texts(texts: list[str]) -> list[str]:
        if not texts:
            raise ValueError("texts must not be empty")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("all texts must be non-empty strings")
        return texts

    @staticmethod
    def _hash_corpus(texts: list[str]) -> str:
        digest = hashlib.sha256()
        for text in texts:
            encoded = text.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _ensure_loaded(self) -> None:
        if self._features is not None:
            return
        if not self.model_path.is_file():
            raise RuntimeError("Local LSA model is not fitted; run the ingestion pipeline first")
        self._load()

    def _save(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.model_path.with_suffix(self.model_path.suffix + ".tmp")
        joblib.dump(
            {
                "model_name": self.model_name,
                "requested_dimensions": self.dimensions,
                "actual_dimensions": self.actual_dimensions,
                "corpus_hash": self._corpus_hash,
                "features": self._features,
                "svd": self._svd,
                "normalizer": self._normalizer,
            },
            temporary,
        )
        temporary.replace(self.model_path)

    def _load(self) -> None:
        values = joblib.load(self.model_path)
        if values.get("model_name") != self.model_name:
            raise RuntimeError("Stored embedding model name does not match configuration")
        if values.get("requested_dimensions") != self.dimensions:
            raise RuntimeError("Stored embedding dimensions do not match configuration")
        self.actual_dimensions = int(values["actual_dimensions"])
        self._corpus_hash = str(values["corpus_hash"])
        self._features = values["features"]
        self._svd = values["svd"]
        self._normalizer = values["normalizer"]
