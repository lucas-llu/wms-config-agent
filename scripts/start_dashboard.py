"""Start the local Streamlit Dashboard from the repository root."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def dashboard_command() -> list[str]:
    app_path = Path(__file__).parents[1] / "src" / "observability" / "dashboard" / "app.py"
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address=127.0.0.1",
    ]


def main() -> None:
    subprocess.run(dashboard_command(), check=True)


if __name__ == "__main__":
    main()
