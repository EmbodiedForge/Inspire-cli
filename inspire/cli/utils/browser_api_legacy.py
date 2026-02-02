"""Legacy browser (web-session) API implementation.

This module intentionally keeps the original implementation together for stability.
New code should import via `inspire.cli.utils.browser_api` (façade) or the domain modules.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from inspire.cli.utils.browser_api_core import (
    BASE_URL,
    _browser_api_path,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _request_json,
    _run_in_thread,
)
from inspire.cli.utils.browser_api_jobs import (  # noqa: F401
    JobInfo,
    get_current_user,
    list_job_users,
    list_jobs,
)
from inspire.cli.utils.browser_api_availability import (  # noqa: F401
    FullFreeNodeCount,
    GPUAvailability,
    find_best_compute_group_accurate,
    get_accurate_gpu_availability,
    get_full_free_node_counts,
    list_compute_groups,
)
from inspire.cli.utils.browser_api_projects import (  # noqa: F401
    ProjectInfo,
    list_projects,
    select_project,
)
from .web_session import (  # noqa: E402
    DEFAULT_WORKSPACE_ID,
    WebSession,
    build_requests_session,
    get_web_session,
)

# =============================================================================
# Notebook (Interactive Modeling) APIs
# =============================================================================


@dataclass
class ImageInfo:
    """Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images.

    Args:
        workspace_id: Workspace to list images from.
        source: Image source filter (default: "SOURCE_OFFICIAL").
        session: Optional pre-existing web session.

    Returns:
        List of ImageInfo objects.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page": 0,
        "page_size": -1,
        "filter": {
            "source": source,
            "source_list": [],
            "registry_hint": {"workspace_id": workspace_id},
        },
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/image/list"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("images", [])
    results = []
    for item in items:
        # Parse image name and version from URL
        url = item.get("address", "")
        name = item.get("name", url.split("/")[-1] if url else "")
        framework = item.get("framework", "")
        version = item.get("version", "")

        results.append(
            ImageInfo(
                image_id=item.get("image_id", ""),
                url=url,
                name=name,
                framework=framework,
                version=version,
            )
        )
    return results


def get_notebook_schedule(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Get notebook schedule configuration including resource specs.

    Args:
        workspace_id: Workspace to get schedule for.
        session: Optional pre-existing web session.

    Returns:
        Schedule configuration dictionary with predef_train_spec and quota data.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/notebook/schedule/{workspace_id}"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups available for interactive notebooks.

    Args:
        workspace_id: Workspace to list groups from.
        session: Optional pre-existing web session.

    Returns:
        List of compute group dictionaries with GPU availability info.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page_num": 1,
        "page_size": -1,
        "filter": {
            "workspace_id": workspace_id,
            "support_job_type": "interactive_modeling",
            "include_gpu_type_stats": True,
        },
        "sorter": [],
    }

    data = _request_json(
        session,
        "POST",
        "/api/v1/logic_compute_groups/list",
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )
    return data.get("data", {}).get("logic_compute_groups", [])


def create_notebook(
    name: str,
    project_id: str,
    project_name: str,
    image_id: str,
    image_url: str,
    logic_compute_group_id: str,
    quota_id: str,
    gpu_type: str,
    gpu_count: int = 1,
    cpu_count: int = 20,
    memory_size: int = 200,
    shared_memory_size: int = 0,
    auto_stop: bool = False,
    priority: int = 10,
    vscode_version: str = "1.101.2",
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Create a new interactive notebook instance.

    Args:
        name: Name for the notebook instance.
        project_id: Project ID to associate with.
        project_name: Project name.
        image_id: Docker image ID (mirror_id).
        image_url: Docker image URL (mirror_url).
        logic_compute_group_id: Compute group ID.
        quota_id: Resource quota/spec ID.
        gpu_type: GPU type string (e.g., "NVIDIA_H200_SXM_141G").
        gpu_count: Number of GPUs (default: 1).
        cpu_count: Number of CPUs (default: 20).
        memory_size: Memory in GB (default: 200).
        shared_memory_size: Shared memory (/dev/shm) in GB (default: 0).
        auto_stop: Auto-stop when idle (default: False).
        priority: Task priority (default: 10).
        vscode_version: VS Code version (default: "1.101.2").
        workspace_id: Workspace ID.
        session: Optional pre-existing web session.

    Returns:
        API response with notebook_id.
    """
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "workspace_id": workspace_id,
        "name": name,
        "project_id": project_id,
        "project_name": project_name,
        "auto_stop": auto_stop,
        "mirror_id": image_id,
        "mirror_url": image_url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "cpu_count": cpu_count,
        "gpu_count": gpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shared_memory_size,
        "resource_spec_price": {
            "cpu_type": "",
            "cpu_count": cpu_count,
            "gpu_type": gpu_type,
            "gpu_count": gpu_count,
            "memory_size_gib": memory_size,
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
        },
        "task_priority": priority,
        "vscode_version": vscode_version,
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/create"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=60,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def stop_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Stop a running notebook instance.

    Args:
        notebook_id: ID of the notebook to stop.
        session: Optional pre-existing web session.

    Returns:
        API response.
    """
    if session is None:
        session = get_web_session()

    body = {
        "notebook_id": notebook_id,
        "operation": "STOP",
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/operate"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def start_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Start a stopped notebook instance.

    Args:
        notebook_id: ID of the notebook to start.
        session: Optional pre-existing web session.

    Returns:
        API response.
    """
    if session is None:
        session = get_web_session()

    body = {
        "notebook_id": notebook_id,
        "operation": "START",
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/operate"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def get_notebook_detail(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Get detailed notebook information.

    Args:
        notebook_id: Notebook instance ID (UUID).
        session: Optional pre-existing web session.

    Returns:
        Notebook detail dictionary.
    """
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/notebook/{notebook_id}"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def wait_for_notebook_running(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> dict:
    """Wait for a notebook instance to reach RUNNING status.

    Args:
        notebook_id: Notebook instance ID.
        session: Optional pre-existing web session.
        timeout: Max wait time in seconds.
        poll_interval: Poll interval in seconds.

    Returns:
        Notebook detail dictionary when RUNNING.

    Raises:
        TimeoutError: If notebook does not become RUNNING within timeout.
    """
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None

    while True:
        notebook = get_notebook_detail(notebook_id=notebook_id, session=session)
        status = (notebook.get("status") or "").upper()
        if status:
            last_status = status

        if status == "RUNNING":
            return notebook

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Notebook '{notebook_id}' did not reach RUNNING within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Ensure the notebook exposes an rtunnel server via Jupyter proxy.

    This automates the JupyterLab UI to:
    1) Open the notebook IDE (JupyterLab)
    2) Open a terminal
    3) (Optional) Install an SSH public key into ~/.ssh/authorized_keys
    4) Start sshd (port `ssh_port`) and rtunnel server (port `port`)

    Returns:
        HTTPS proxy URL for the rtunnel WebSocket endpoint (to be used as PROXY_URL).
    """
    if _in_asyncio_loop():
        return _run_in_thread(
            _setup_notebook_rtunnel_sync,
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        session=session,
        headless=headless,
        timeout=timeout,
    )


def _setup_notebook_rtunnel_sync(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Sync implementation for setup_notebook_rtunnel."""
    from playwright.sync_api import sync_playwright
    import sys as _sys

    if session is None:
        session = get_web_session()

    # Fast-path: Try to connect to rtunnel via known proxy URL pattern first.
    # This avoids slow browser automation if rtunnel is already running.
    notebook_lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}/proxy/{port}/")
    known_proxy_url = f"{BASE_URL}{notebook_lab_path}"
    try:
        http = build_requests_session(session, BASE_URL)
        resp = http.get(known_proxy_url, timeout=5)
        body = resp.text[:200] if resp.text else ""
        # Only use fast path if we get a valid response (not 401/302 auth redirects)
        # rtunnel returns 200 with specific body when running
        if resp.status_code == 200 and "ECONNREFUSED" not in body and "<html>" not in body.lower():
            _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
            _sys.stderr.flush()
            http.close()
            return known_proxy_url
        http.close()
    except Exception:
        pass  # Fall through to browser automation

    _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            page.goto(
                f"{BASE_URL}/ide?notebook_id={notebook_id}",
                timeout=60000,
                wait_until="domcontentloaded",
            )

            # Find the embedded JupyterLab frame (notebook-inspire host).
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

            jupyter_url = lab_frame.url
            notebook_lab_pattern = _browser_api_path("/notebook/lab/")
            # Check if the URL contains the notebook lab path pattern
            if notebook_lab_pattern.lstrip("/") in jupyter_url:
                from urllib.parse import urlsplit, urlunsplit

                parsed = urlsplit(jupyter_url)
                base_path = parsed.path
                if not base_path.endswith("/"):
                    base_path = base_path + "/"
                base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
                jupyter_proxy_url = f"{base_url}proxy/{port}/"
            else:
                jupyter_proxy_url = jupyter_url.rstrip("/")
                if jupyter_proxy_url.endswith("/lab"):
                    jupyter_proxy_url = jupyter_proxy_url[:-4]
                jupyter_proxy_url = f"{jupyter_proxy_url}/proxy/{port}/"

            # Wait for JupyterLab UI to be ready.
            # The IDE page often shows a full-screen loading overlay ("加载中...")
            # before the JupyterLab menu bar becomes available.
            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=180000)
            except Exception:
                pass

            # Prefer the launcher terminal card (it appears earlier than the menu bar in some builds).
            try:
                lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                ).first.wait_for(
                    state="visible",
                    timeout=180000,
                )
            except Exception:
                try:
                    lab_frame.get_by_role("menuitem", name="File").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )
                except Exception:
                    lab_frame.get_by_role("menuitem", name="文件").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )

            # Dismiss Jupyter news prompt if present.
            for label in ("No", "Yes", "否", "不接收", "取消"):
                try:
                    btn = lab_frame.get_by_role("button", name=label)
                    if btn.count() > 0:
                        # Prefer closing the prompt (No), but any click removes overlay.
                        btn.first.click(timeout=1000)
                        break
                except Exception:
                    pass

            # Open a terminal.
            terminal_opened = False

            # Path A: Launcher card
            terminal_card = lab_frame.locator(
                "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
            )
            try:
                terminal_card.first.wait_for(state="visible", timeout=20000)
                terminal_card.first.click(timeout=8000)
                terminal_opened = True
            except Exception:
                terminal_opened = False

            # Path B: Open Launcher then click Terminal
            if not terminal_opened:
                try:
                    launcher_btn = lab_frame.locator(
                        "button[title*='Launcher'], button[aria-label*='Launcher']"
                    ).first
                    if launcher_btn.count() > 0:
                        launcher_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                    terminal_card = lab_frame.locator(
                        "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                    )
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            # Path C: File -> New -> Terminal
            if not terminal_opened:
                try:
                    try:
                        lab_frame.get_by_role("menuitem", name="File").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="New").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="Terminal").first.click(timeout=5000)
                    except Exception:
                        lab_frame.get_by_role("menuitem", name="文件").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="新建").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="终端").first.click(timeout=5000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            # Ensure terminal tab is active before typing.
            try:
                term_tab = lab_frame.locator(
                    "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
                ).first
                if term_tab.count() > 0:
                    term_tab.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            # Focus terminal input to ensure keystrokes land in the shell.
            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea, .xterm, .jp-Terminal"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            # Run setup via terminal commands.
            # Make sure we are at a clean prompt (avoid being stuck in a multiline quote).
            try:
                page.keyboard.press("Control+C")
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
            except Exception:
                pass

            # Use the same nightly tarball as the local tunnel client.
            try:
                from inspire.cli.utils.tunnel import _get_rtunnel_download_url

                RTUNNEL_DOWNLOAD_URL = _get_rtunnel_download_url()
            except Exception:
                RTUNNEL_DOWNLOAD_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"

            import shlex

            cmd_lines: list[str] = []

            pip_index_url = os.environ.get("INSPIRE_PIP_INDEX_URL")
            pip_trusted_host = os.environ.get("INSPIRE_PIP_TRUSTED_HOST")
            apt_mirror_url = os.environ.get("INSPIRE_APT_MIRROR_URL")
            rtunnel_bin = os.environ.get("INSPIRE_RTUNNEL_BIN")
            sshd_deb_dir = os.environ.get("INSPIRE_SSHD_DEB_DIR")
            dropbear_deb_dir = os.environ.get("INSPIRE_DROPBEAR_DEB_DIR")

            if pip_index_url:
                cmd_lines.append(f"pip config set global.index-url {shlex.quote(pip_index_url)}")
                if pip_trusted_host:
                    cmd_lines.append(
                        f"pip config set global.trusted-host {shlex.quote(pip_trusted_host)}"
                    )
            elif pip_trusted_host:
                cmd_lines.append(
                    f"pip config set global.trusted-host {shlex.quote(pip_trusted_host)}"
                )

            if apt_mirror_url:
                cmd_lines.extend(
                    [
                        "echo '>>> configure apt source...'",
                        'CODENAME=$( . /etc/os-release && echo "$VERSION_CODENAME" )',
                        "cat >/etc/apt/sources.list.d/ubuntu.sources <<EOF",
                        "Types: deb",
                        f"URIs: {apt_mirror_url}",
                        "Suites: ${CODENAME} ${CODENAME}-updates ${CODENAME}-backports ${CODENAME}-security",
                        "Components: main restricted universe multiverse",
                        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg",
                        "EOF",
                        "echo '>>> update apt cache...'",
                        "apt-get update -y -qq || apt-get update -y",
                    ]
                )

            if rtunnel_bin:
                cmd_lines.append(f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin)}")
                cmd_lines.append(
                    'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi'
                )

            if sshd_deb_dir:
                cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(sshd_deb_dir)}")
                cmd_lines.append(
                    'if [ -d "$SSHD_DEB_DIR" ]; then for _i in 1 2 3; do dpkg -i "$SSHD_DEB_DIR"/*.deb && break; done; ldconfig >/dev/null 2>&1 || true; fi'
                )

            if dropbear_deb_dir:
                cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(dropbear_deb_dir)}")

            if ssh_public_key:
                cmd_lines.extend(
                    [
                        "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                        "cat >> ~/.ssh/authorized_keys <<'EOF'",
                        ssh_public_key.rstrip(),
                        "EOF",
                        "chmod 600 ~/.ssh/authorized_keys",
                    ]
                )

            # Use the setup script from shared path if dropbear is requested
            if dropbear_deb_dir:
                setup_script = os.environ.get("INSPIRE_SETUP_SCRIPT")
                if not setup_script:
                    raise ValueError(
                        "INSPIRE_SETUP_SCRIPT environment variable is required when using dropbear. "
                        "Set it to the path of your SSH setup script on the cluster."
                    )
                rtunnel_bin_arg = rtunnel_bin or "/tmp/rtunnel"
                cmd_lines.extend(
                    [
                        f"PORT={port}",
                        f"SSH_PORT={ssh_port}",
                        "echo '>>> Running SSH setup script...'",
                        f"bash {shlex.quote(setup_script)} {shlex.quote(dropbear_deb_dir)} {shlex.quote(rtunnel_bin_arg)} \"$SSH_PORT\" \"$PORT\" >/tmp/setup_ssh.log 2>&1; tail -80 /tmp/setup_ssh.log; echo '>>> dropbear log'; tail -60 /tmp/dropbear.log 2>/dev/null || true; echo '>>> rtunnel log'; tail -60 /tmp/rtunnel-server.log 2>/dev/null || true",
                        "sleep 2",
                        "echo '>>> Setup script done'",
                    ]
                )
            else:
                # OpenSSH fallback
                cmd_lines.extend(
                    [
                        f"RTUNNEL_URL={RTUNNEL_DOWNLOAD_URL!r}",
                        f"PORT={port}",
                        f"SSH_PORT={ssh_port}",
                        'if [ ! -x /usr/sbin/sshd ] && [ -z "${SSHD_DEB_DIR:-}" ]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq openssh-server; fi',
                        "pkill -f 'sshd -p' 2>/dev/null || true",
                        'if [ -x /usr/sbin/sshd ]; then mkdir -p /run/sshd && chmod 0755 /run/sshd; ssh-keygen -A >/dev/null 2>&1 || true; /usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes -o PasswordAuthentication=no -o PubkeyAuthentication=yes >/dev/null 2>&1 & fi',
                        # rtunnel for OpenSSH
                        "RTUNNEL_BIN=/tmp/rtunnel",
                        'if [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi',
                        'pkill -f "rtunnel.*:$PORT" 2>/dev/null || true',
                        f"if [ ! -x \"$RTUNNEL_BIN\" ]; then curl -fsSL '{RTUNNEL_DOWNLOAD_URL}' -o /tmp/rtunnel.tgz && tar -xzf /tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel 2>/dev/null; fi",
                        'nohup "$RTUNNEL_BIN" "127.0.0.1:$SSH_PORT" "0.0.0.0:$PORT" >/tmp/rtunnel-server.log 2>&1 &',
                    ]
                )

            _sys.stderr.write("  Executing setup commands in terminal...\n")
            _sys.stderr.flush()
            for line in cmd_lines:
                page.keyboard.type(line, delay=2)
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)

            # Wait for script to complete and take debug screenshot
            _sys.stderr.write("  Waiting for services to start...\n")
            _sys.stderr.flush()
            page.wait_for_timeout(5000)
            try:
                page.screenshot(path="/tmp/notebook_terminal_debug.png")
            except Exception:
                pass

            # Derive proxy URL (prefer VSCode/code-server proxy).
            proxy_url = None
            try:
                vscode_tab = page.locator('img[alt="vscode"]').first
                if vscode_tab.count() > 0:
                    vscode_tab.click(timeout=5000)
                    page.wait_for_timeout(3000)

                vscode_url = None
                for fr in page.frames:
                    if "/vscode/" in fr.url:
                        vscode_url = fr.url
                        break

                if vscode_url:
                    from urllib.parse import urlparse, parse_qs

                    parsed = urlparse(vscode_url)
                    token = parse_qs(parsed.query).get("token", [None])[0]
                    base = vscode_url.split("?", 1)[0].rstrip("/")
                    proxy_url = f"{base}/proxy/{port}/"
                    if token:
                        proxy_url = f"{proxy_url}?token={token}"
            except Exception:
                proxy_url = None

            if not proxy_url:
                proxy_url = jupyter_proxy_url

            # Probe the proxy endpoint until it stops reporting connection refused.
            _sys.stderr.write("  Verifying rtunnel is reachable...\n")
            _sys.stderr.flush()
            start = time.time()
            last_status = None
            last_progress_time = start
            while time.time() - start < timeout:
                elapsed = time.time() - start
                # Print progress every 30 seconds
                if time.time() - last_progress_time >= 30:
                    _sys.stderr.write(f"  Waiting for rtunnel... ({int(elapsed)}s elapsed)\n")
                    _sys.stderr.flush()
                    last_progress_time = time.time()
                try:
                    resp = context.request.get(proxy_url, timeout=5000)
                    body = ""
                    try:
                        body = resp.text()
                    except Exception:
                        body = ""
                    last_status = f"{resp.status} {body[:200].strip()}"
                    if "ECONNREFUSED" not in body:
                        return proxy_url
                except Exception as e:
                    last_status = str(e)

                page.wait_for_timeout(1000)

            # Build detailed error message with debugging hints
            error_msg = (
                f"rtunnel server did not become reachable within {timeout}s.\n"
                f"Last response: {last_status}\n\n"
                "Debugging hints:\n"
                "  1. Check if rtunnel binary is present: ls -la /tmp/rtunnel\n"
                "  2. Check rtunnel server log: cat /tmp/rtunnel-server.log\n"
                "  3. Check if sshd/dropbear is running: ps aux | grep -E 'sshd|dropbear'\n"
                "  4. Check dropbear log: cat /tmp/dropbear.log\n"
                "  5. Try running with --debug-playwright to see the browser\n"
                f"  6. Screenshot saved to /tmp/notebook_terminal_debug.png"
            )
            raise ValueError(error_msg)

        finally:
            try:
                context.close()
            finally:
                browser.close()


def run_command_in_notebook(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> None:
    """Run a command in a notebook's Jupyter terminal.

    This uses browser automation to open JupyterLab, open a terminal,
    and type the command.

    Args:
        notebook_id: Notebook instance ID (UUID).
        command: Shell command to run in the terminal.
        session: Optional pre-existing web session.
        headless: Run browser headlessly (default: True).
        timeout: Timeout in seconds for the operation.

    Raises:
        ValueError: If terminal cannot be opened or command fails.
    """
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _run_command_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        headless=headless,
        timeout=timeout,
    )


def _run_command_in_notebook_sync(
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
) -> None:
    """Sync implementation for run_command_in_notebook."""
    from playwright.sync_api import sync_playwright
    import sys as _sys

    if session is None:
        session = get_web_session()

    _sys.stderr.write("Running command in notebook terminal...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()

        try:
            page.goto(
                f"{BASE_URL}/ide?notebook_id={notebook_id}",
                timeout=60000,
                wait_until="domcontentloaded",
            )

            # Find the embedded JupyterLab frame
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

            # Wait for JupyterLab UI to be ready
            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=180000)
            except Exception:
                pass

            # Wait for launcher or menu
            try:
                lab_frame.locator(
                    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                ).first.wait_for(
                    state="visible",
                    timeout=180000,
                )
            except Exception:
                try:
                    lab_frame.get_by_role("menuitem", name="File").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )
                except Exception:
                    lab_frame.get_by_role("menuitem", name="文件").first.wait_for(
                        state="visible",
                        timeout=180000,
                    )

            # Dismiss Jupyter news prompt if present
            for label in ("No", "Yes", "否", "不接收", "取消"):
                try:
                    btn = lab_frame.get_by_role("button", name=label)
                    if btn.count() > 0:
                        btn.first.click(timeout=1000)
                        break
                except Exception:
                    pass

            # Open a terminal
            terminal_opened = False

            # Path A: Launcher card
            terminal_card = lab_frame.locator(
                "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
            )
            try:
                terminal_card.first.wait_for(state="visible", timeout=20000)
                terminal_card.first.click(timeout=8000)
                terminal_opened = True
            except Exception:
                terminal_opened = False

            # Path B: Open Launcher then click Terminal
            if not terminal_opened:
                try:
                    launcher_btn = lab_frame.locator(
                        "button[title*='Launcher'], button[aria-label*='Launcher']"
                    ).first
                    if launcher_btn.count() > 0:
                        launcher_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                    terminal_card = lab_frame.locator(
                        "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
                    )
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            # Path C: File -> New -> Terminal
            if not terminal_opened:
                try:
                    try:
                        lab_frame.get_by_role("menuitem", name="File").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="New").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="Terminal").first.click(timeout=5000)
                    except Exception:
                        lab_frame.get_by_role("menuitem", name="文件").first.click(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="新建").first.hover(timeout=3000)
                        lab_frame.get_by_role("menuitem", name="终端").first.click(timeout=5000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            # Ensure terminal tab is active
            try:
                term_tab = lab_frame.locator(
                    "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
                ).first
                if term_tab.count() > 0:
                    term_tab.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            # Focus terminal input
            try:
                term_focus = lab_frame.locator(
                    "textarea.xterm-helper-textarea, .xterm, .jp-Terminal"
                ).first
                if term_focus.count() > 0:
                    term_focus.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            # Make sure we are at a clean prompt
            try:
                page.keyboard.press("Control+C")
                page.keyboard.press("Enter")
                page.wait_for_timeout(100)
            except Exception:
                pass

            # Type and execute the command
            _sys.stderr.write("  Executing command...\n")
            _sys.stderr.flush()
            page.keyboard.type(command, delay=2)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)

            _sys.stderr.write("  Command sent successfully.\n")
            _sys.stderr.flush()

        finally:
            try:
                context.close()
            finally:
                browser.close()
