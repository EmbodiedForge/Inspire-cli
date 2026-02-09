"""Tests for notebook Jupyter URL helpers."""

from __future__ import annotations

from inspire.platform.web.browser_api.playwright_notebooks import build_jupyter_proxy_url


def test_build_jupyter_proxy_url_includes_token_from_path() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=token-abc")


def test_build_jupyter_proxy_url_prefers_query_token() -> None:
    lab_url = (
        "https://nat-notebook-inspire.sii.edu.cn/ws-xxx/project-yyy/user-zzz/"
        "jupyter/notebook-123/token-abc/lab?token=query-token"
    )

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url.endswith("/proxy/31337/?token=query-token")


def test_build_jupyter_proxy_url_notebook_lab_pattern() -> None:
    lab_url = "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/"

    proxy_url = build_jupyter_proxy_url(lab_url, port=31337)

    assert proxy_url == "https://qz.sii.edu.cn/api/v1/notebook/lab/notebook-123/proxy/31337/"
