from pathlib import Path

from libs.loader import PdfLoader
from libs.loader import pdf_loader as pdf_loader_module


def _write_minimal_text_pdf(path: Path, text: str) -> None:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode())
        content.extend(payload)
        content.extend(b"\nendobj\n")
    xref_offset = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(content)


def test_pdf_loader_extracts_text_and_page_metadata(tmp_path: Path) -> None:
    source = tmp_path / "moca.pdf"
    _write_minimal_text_pdf(source, "MOCA policy configuration")

    document = PdfLoader(image_output_dir=tmp_path / "images").load(
        source, {"module": "policy", "version": "2024.1"}
    )

    assert "MOCA policy configuration" in document.text
    assert document.metadata["source_path"].endswith("moca.pdf")
    assert document.metadata["page_count"] == 1
    assert document.metadata["pages"][0]["page"] == 1
    assert document.metadata["module"] == "policy"


def test_pdf_loader_extracts_images_and_degrades_per_image(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "with-images.pdf"
    source.write_bytes(b"fake-pdf-for-mocked-reader")

    class FakeImage:
        name = "unsafe/path/diagram.png"
        data = b"image-bytes"

    class FakeImages:
        def keys(self):
            return ["good", "broken"]

        def __getitem__(self, name):
            if name == "broken":
                raise ValueError("broken image stream")
            return FakeImage()

    class FakePage:
        images = FakeImages()

        @staticmethod
        def extract_text():
            return "Configuration diagram"

    class FakeReader:
        pages = [FakePage()]

    monkeypatch.setattr(pdf_loader_module, "PdfReader", lambda _: FakeReader())

    document = PdfLoader(image_output_dir=tmp_path / "images").load(source)

    assert "[IMAGE:" in document.text
    assert len(document.metadata["images"]) == 1
    image = document.metadata["images"][0]
    assert Path(image["path"]).read_bytes() == b"image-bytes"
    assert image["page"] == 1
    assert image["text_offset"] == document.text.index("[IMAGE:")
