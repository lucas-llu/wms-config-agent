"""Build a private, content-addressed catalog for the WMS PDF corpus."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from libs.atomic_file import replace_file_atomically
from libs.loader.base_loader import BaseLoader

_PROCESS_CODE = re.compile(r"SWL\.[IOSV]\.\d+\.\d+", re.IGNORECASE)
_CONFIGURATION_WORD = re.compile(r"configur[a-z]*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CorpusManifestEntry:
    """Metadata needed to ingest and cite one source PDF."""

    schema_version: int
    document_id: str
    file_hash: str
    source_path: str
    source_name: str
    title: str
    process_code: str
    domain: str
    process_stage: str
    document_type: str
    page_count: int
    size_bytes: int
    modified_at: str
    version: str
    related_document_paths: tuple[str, ...] = ()
    duplicate_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CorpusManifestSummary:
    total_documents: int
    unique_content_files: int
    duplicate_files: int
    configuration_documents: int
    operation_documents: int
    unique_process_codes: int
    paired_process_codes: int
    total_pages: int
    total_bytes: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class CorpusManifestBuilder:
    """Scan PDFs without writing extracted text or images."""

    def build_entry(
        self,
        source_path: str | Path,
        *,
        source_root: str | Path | None = None,
    ) -> CorpusManifestEntry:
        """Inspect one PDF for an interactive ingestion request."""
        path = Path(source_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Corpus document does not exist: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError("Interactive corpus ingestion currently accepts PDF files only")
        root = Path(source_root).resolve() if source_root is not None else path.parent
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("source_path must be inside source_root") from exc
        parent_parts = Path(relative_path).parts[:-1]
        domain = parent_parts[0] if parent_parts else "Unclassified"
        process_stage = parent_parts[1] if len(parent_parts) > 1 else domain
        file_hash = BaseLoader.compute_file_hash(path)
        try:
            process_code = self._process_code(path.stem)
        except ValueError:
            process_code = "UNSPECIFIED"
        return CorpusManifestEntry(
            schema_version=1,
            document_id=BaseLoader.build_document_id(file_hash),
            file_hash=file_hash,
            source_path=relative_path,
            source_name=path.name,
            title=self._title(path.stem) or path.stem,
            process_code=process_code,
            domain=domain,
            process_stage=process_stage,
            document_type=self._document_type(path.stem),
            page_count=len(PdfReader(path, strict=False).pages),
            size_bytes=path.stat().st_size,
            modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            version="unspecified",
        )

    def scan(self, source_root: str | Path) -> list[CorpusManifestEntry]:
        root = Path(source_root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Corpus directory does not exist: {root}")

        entries: list[CorpusManifestEntry] = []
        first_path_by_hash: dict[str, str] = {}
        for path in sorted(root.rglob("*.pdf"), key=lambda item: item.as_posix().lower()):
            relative_path = path.relative_to(root).as_posix()
            file_hash = BaseLoader.compute_file_hash(path)
            duplicate_of = first_path_by_hash.get(file_hash)
            first_path_by_hash.setdefault(file_hash, relative_path)
            process_code = self._process_code(path.stem)
            parent_parts = path.relative_to(root).parts[:-1]
            domain = parent_parts[0] if parent_parts else "Unclassified"
            process_stage = parent_parts[1] if len(parent_parts) > 1 else domain
            entries.append(
                CorpusManifestEntry(
                    schema_version=1,
                    document_id=BaseLoader.build_document_id(file_hash),
                    file_hash=file_hash,
                    source_path=relative_path,
                    source_name=path.name,
                    title=self._title(path.stem),
                    process_code=process_code,
                    domain=domain,
                    process_stage=process_stage,
                    document_type=self._document_type(path.stem),
                    page_count=len(PdfReader(path, strict=False).pages),
                    size_bytes=path.stat().st_size,
                    modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                    version="unspecified",
                    duplicate_of=duplicate_of,
                )
            )

        paths_by_code: dict[str, list[str]] = {}
        for entry in entries:
            paths_by_code.setdefault(entry.process_code, []).append(entry.source_path)
        return [
            replace(
                entry,
                related_document_paths=tuple(
                    path for path in paths_by_code[entry.process_code] if path != entry.source_path
                ),
            )
            for entry in entries
        ]

    @staticmethod
    def summarize(entries: list[CorpusManifestEntry]) -> CorpusManifestSummary:
        codes: dict[str, set[str]] = {}
        for entry in entries:
            codes.setdefault(entry.process_code, set()).add(entry.document_type)
        return CorpusManifestSummary(
            total_documents=len(entries),
            unique_content_files=len({entry.file_hash for entry in entries}),
            duplicate_files=sum(entry.duplicate_of is not None for entry in entries),
            configuration_documents=sum(
                entry.document_type == "configuration" for entry in entries
            ),
            operation_documents=sum(entry.document_type == "operation" for entry in entries),
            unique_process_codes=len(codes),
            paired_process_codes=sum(
                {"configuration", "operation"}.issubset(document_types)
                for document_types in codes.values()
            ),
            total_pages=sum(entry.page_count for entry in entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
        )

    @staticmethod
    def write(entries: list[CorpusManifestEntry], output_path: str | Path) -> Path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        payload = "".join(
            json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for entry in entries
        )
        temporary.write_text(payload, encoding="utf-8")
        replace_file_atomically(temporary, destination)
        return destination

    @staticmethod
    def read(path: str | Path) -> list[CorpusManifestEntry]:
        entries: list[CorpusManifestEntry] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            values = json.loads(line)
            values["related_document_paths"] = tuple(values["related_document_paths"])
            entries.append(CorpusManifestEntry(**values))
        return entries

    @staticmethod
    def _process_code(stem: str) -> str:
        match = _PROCESS_CODE.search(stem)
        if not match:
            raise ValueError(f"Cannot derive process code from filename: {stem}")
        return match.group(0).upper()

    @staticmethod
    def _document_type(stem: str) -> str:
        return "configuration" if _CONFIGURATION_WORD.search(stem) else "operation"

    @staticmethod
    def _title(stem: str) -> str:
        title = _PROCESS_CODE.sub("", stem, count=1).lstrip(". ")
        title = re.sub(
            r"\s*-?\s*configur[a-z]*\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return title.strip(" .-")
