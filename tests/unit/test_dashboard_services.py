from __future__ import annotations

import sys
from pathlib import Path

from ingestion import DocumentDetail, DocumentInfo
from ingestion.storage import StoredImage
from observability.dashboard.services import ConfigService, DataService
from scripts.start_dashboard import dashboard_command


class _UnusedManager:
    pass


def test_config_service_summarizes_components_without_credentials() -> None:
    service = ConfigService.from_path("config/settings.yaml")

    serialized = repr([component.to_dict() for component in service.components()])

    assert service.project_summary()["name"] == "wms-config-agent"
    assert "openai_compatible" in serialized
    assert "WMS_LLM_API_KEY" not in serialized
    assert "https://" not in serialized


def test_data_service_only_previews_allowlisted_managed_images(tmp_path: Path) -> None:
    image_root = tmp_path / "images"
    image_root.mkdir()
    safe = image_root / "safe.png"
    unsafe_suffix = image_root / "payload.svg"
    outside = tmp_path / "outside.png"
    safe.write_bytes(b"png")
    unsafe_suffix.write_text("<svg/>", encoding="utf-8")
    outside.write_bytes(b"png")
    document = DocumentInfo("doc", "manual.pdf", "manuals", 1, 3, None, "hash", None)
    detail = DocumentDetail(
        document,
        (),
        (
            StoredImage("safe", safe, "manuals", "hash", 1, "now"),
            StoredImage("suffix", unsafe_suffix, "manuals", "hash", 1, "now"),
            StoredImage("outside", outside, "manuals", "hash", 1, "now"),
        ),
    )
    service = DataService(_UnusedManager(), image_root=image_root)  # type: ignore[arg-type]

    assert [image["image_id"] for image in service.previewable_images(detail)] == ["safe"]


def test_dashboard_command_uses_current_python_and_absolute_app_path() -> None:
    command = dashboard_command()

    assert command[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert Path(command[4]).is_absolute()
    assert Path(command[4]).name == "app.py"
