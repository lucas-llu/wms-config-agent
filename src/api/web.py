"""Serve only built UI assets; never the workspace, corpus or private exports."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_workbench(app, directory, issuer):
    root = Path(directory).resolve()
    if not (root / "index.html").is_file() or not (root / "assets").is_dir():
        raise ValueError("Build the React workbench before serving it")
    url = urlsplit(issuer)
    origin = f"{url.scheme}://{url.netloc}"
    policy = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {origin}; frame-src {origin}; img-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="workbench-assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(
            root / "index.html",
            media_type="text/html",
            headers={"Content-Security-Policy": policy, "Referrer-Policy": "no-referrer"},
        )
