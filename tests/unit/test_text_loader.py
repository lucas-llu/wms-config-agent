from pathlib import Path

import pytest

from libs.loader import LoaderFactory, TextLoader


def test_markdown_front_matter_becomes_domain_metadata(tmp_path: Path) -> None:
    source = tmp_path / "allocation.md"
    source.write_text(
        """---
title: Allocation policy
version: "2024.1"
module: allocation
site: DC01
environment: test
---
# Allocation

Configure the policy in the authorized test environment.
""",
        encoding="utf-8",
    )

    first = TextLoader().load(source, {"collection": "moca-guides"})
    second = TextLoader().load(source, {"collection": "moca-guides"})

    assert first.id == second.id
    assert first.text.startswith("# Allocation")
    assert first.metadata["module"] == "allocation"
    assert first.metadata["site"] == "DC01"
    assert first.metadata["version"] == "2024.1"
    assert first.metadata["collection"] == "moca-guides"
    assert first.metadata["source_path"].endswith("allocation.md")


def test_text_loader_accepts_utf8_bom(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("MOCA 命令说明", encoding="utf-8-sig")

    document = TextLoader().load(source)

    assert document.text == "MOCA 命令说明"
    assert document.metadata["doc_type"] == "text"


def test_loader_factory_routes_extensions() -> None:
    assert isinstance(LoaderFactory.create("guide.md"), TextLoader)
    with pytest.raises(ValueError, match="Unsupported document type"):
        LoaderFactory.create("archive.zip")
