import pytest

from core.settings import SplitterSettings
from libs.splitter import BaseSplitter, RecursiveSplitter, SplitterFactory


class FakeSplitter(BaseSplitter):
    def split_text(self, text: str, trace=None) -> list[str]:
        return [text]


def test_factory_creates_recursive_splitter() -> None:
    settings = SplitterSettings(provider="recursive", chunk_size=120, chunk_overlap=20)

    splitter = SplitterFactory.create(settings)

    assert isinstance(splitter, RecursiveSplitter)
    assert splitter.chunk_size == 120


def test_factory_supports_registered_provider(monkeypatch) -> None:
    monkeypatch.setitem(
        SplitterFactory._providers,
        "fake",
        lambda chunk_size, chunk_overlap: FakeSplitter(),
    )
    settings = SplitterSettings(provider="fake", chunk_size=10, chunk_overlap=0)

    assert isinstance(SplitterFactory.create(settings), FakeSplitter)


def test_factory_rejects_unknown_provider() -> None:
    settings = SplitterSettings(provider="semantic", chunk_size=100, chunk_overlap=10)
    with pytest.raises(ValueError, match="Unknown splitter provider 'semantic'"):
        SplitterFactory.create(settings)
