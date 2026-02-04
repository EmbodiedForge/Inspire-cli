"""Jupyter navigation helpers for notebook Playwright flows."""

from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

from inspire.cli.utils.browser_api_core import BASE_URL, _browser_api_path


def open_notebook_lab(page, *, notebook_id: str):  # noqa: ANN001
    """Open the notebook's JupyterLab and return the lab frame/page handle."""
    page.goto(
        f"{BASE_URL}/ide?notebook_id={notebook_id}",
        timeout=60000,
        wait_until="domcontentloaded",
    )

    start = time.time()
    lab_frame = None
    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    while time.time() - start < 60:
        for fr in page.frames:
            url = fr.url or ""
            if "notebook-inspire" in url and url.rstrip("/").endswith("/lab"):
                lab_frame = fr
                break
            if notebook_lab_pattern.lstrip("/") in url:
                lab_frame = fr
                break
        if lab_frame:
            break
        page.wait_for_timeout(500)

    if lab_frame is None:
        notebook_lab_prefix = _browser_api_path("/notebook/lab").rstrip("/")
        direct_lab_url = f"{BASE_URL}{notebook_lab_prefix}/{notebook_id}/"
        page.goto(
            direct_lab_url,
            timeout=60000,
            wait_until="domcontentloaded",
        )
        lab_frame = page

    return lab_frame


def build_jupyter_proxy_url(lab_url: str, *, port: int) -> str:
    """Build a Jupyter proxy URL for the given lab URL and port."""
    notebook_lab_pattern = _browser_api_path("/notebook/lab/")
    if notebook_lab_pattern.lstrip("/") in lab_url:
        parsed = urlsplit(lab_url)
        base_path = parsed.path
        if not base_path.endswith("/"):
            base_path = base_path + "/"
        base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        return f"{base_url}proxy/{port}/"

    proxy_url = lab_url.rstrip("/")
    if proxy_url.endswith("/lab"):
        proxy_url = proxy_url[:-4]
    return f"{proxy_url}/proxy/{port}/"
