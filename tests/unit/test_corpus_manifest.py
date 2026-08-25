from pathlib import Path

from pypdf import PdfWriter

from ingestion.corpus_manifest import CorpusManifestBuilder


def _write_pdf(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as output:
        writer.write(output)


def test_manifest_derives_domain_stage_type_and_relationships(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _write_pdf(
        root / "Inbound" / "I1.Pre-Receiving" / "SWL.I.01.01 Appointment Creation.pdf",
        "operation",
    )
    _write_pdf(
        root
        / "Inbound"
        / "I1.Pre-Receiving"
        / "SWL.I.01.01 Appointment Creation - Configurtaion.pdf",
        "configuration",
    )
    _write_pdf(
        root / "Stock Management" / "SWL.S.01.02 RF Inventory Move.pdf",
        "stock",
    )

    builder = CorpusManifestBuilder()
    entries = builder.scan(root)
    summary = builder.summarize(entries)

    assert len(entries) == 3
    appointment_entries = [entry for entry in entries if entry.process_code == "SWL.I.01.01"]
    assert {entry.document_type for entry in appointment_entries} == {
        "configuration",
        "operation",
    }
    assert all(len(entry.related_document_paths) == 1 for entry in appointment_entries)
    assert appointment_entries[0].domain == "Inbound"
    assert appointment_entries[0].process_stage == "I1.Pre-Receiving"
    assert summary.unique_process_codes == 2
    assert summary.paired_process_codes == 1
    assert summary.configuration_documents == 1
    assert summary.operation_documents == 2


def test_manifest_roundtrip_and_content_deduplication(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    first = root / "VAS" / "SWL.V.02.01 Direct Cross Dock.pdf"
    second = root / "VAS" / "SWL.V.03.01 Indirect Cross Dock.pdf"
    _write_pdf(first, "same-content")
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())

    builder = CorpusManifestBuilder()
    entries = builder.scan(root)
    output = builder.write(entries, tmp_path / "manifest.jsonl")
    restored = builder.read(output)

    assert restored == entries
    assert builder.summarize(entries).duplicate_files == 1
    assert sum(entry.duplicate_of is not None for entry in entries) == 1
