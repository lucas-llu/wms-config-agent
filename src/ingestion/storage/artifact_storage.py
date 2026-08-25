"""Allowlisted lifecycle operations for staged and preprocessed document artifacts."""

from __future__ import annotations

from pathlib import Path


class LocalArtifactStorage:
    """Delete exact files only when every target is below an explicitly allowed root."""

    def __init__(self, allowed_roots: list[str | Path]) -> None:
        roots = tuple(dict.fromkeys(Path(root).resolve() for root in allowed_roots))
        if not roots:
            raise ValueError("allowed_roots must not be empty")
        self.allowed_roots = roots

    def remove_files(self, paths: list[str | Path]) -> int:
        targets = tuple(dict.fromkeys(Path(path).resolve() for path in paths))
        for target in targets:
            if not any(self._is_below(target, root) for root in self.allowed_roots):
                raise PermissionError(f"Artifact is outside configured roots: {target}")
            if target.exists() and not target.is_file():
                raise IsADirectoryError(f"Artifact deletion only accepts files: {target}")

        removed = 0
        for target in targets:
            if target.is_file():
                target.unlink()
                removed += 1
        return removed

    @staticmethod
    def _is_below(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True
