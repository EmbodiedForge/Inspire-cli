from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path

import pytest

# Environment variables that override config values at runtime. A developer or
# CI host exporting any of these (very common for this CLI) must not be able to
# change what a test observes.
ENV_OVERRIDE_PREFIXES = ("INSPIRE_", "INSP_")
#: Except the live-suite contract, which is not a config override at all.
#:
#: ``INSPIRE_E2E_*`` is read by ``tests/e2e/live_cli_harness.py`` in test bodies
#: (``require_core_rw_enabled``) and has no configuration precedence, so scrubbing it
#: bought nothing and cost the resource-creating lane entirely: the opt-in flag was
#: deleted before the test could read it, every run skipped, and a skip exits 0 - so
#: the lane reported success for testing nothing and nobody noticed it had never run.
#: Session-scoped ``live_settings`` survived only because it reads the environment
#: earlier than a function-scoped fixture can touch it.
ENV_OVERRIDE_KEEP = ("INSPIRE_E2E_",)

#: The execd daemon refuses to bind a socket path longer than this. It is a product limit, not a test
#: preference: ``sockaddr_un`` carries 108 bytes and ``Daemon._bind_socket`` in
#: ``inspire/platform/execd/agent.py`` keeps a margin for the terminator. Raising it to make a test
#: pass would ship a daemon that fails at ``bind()`` with a bare ``OSError`` instead.
EXEC_SOCKET_PATH_LIMIT = 100
#: The deepest socket path a test builds under a :func:`short_path` root: the daemon's own layout.
#: The broker's ``broker.sock`` is shorter, so checking this one covers both.
_EXEC_SOCKET_SUFFIX = ("state", "daemon.sock")


@pytest.fixture
def short_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A per-test directory short enough to hold an AF_UNIX socket.

    Anything that starts a real execd daemon, or binds a broker socket, has to build its paths
    somewhere short. ``tmp_path`` is not short: pytest roots it at ``tempfile.gettempdir()``, which
    honours an ambient ``TMPDIR``, and a sandbox or CI scratch directory several levels deep (this
    repo's Alma sandbox sets ``TMPDIR`` under ``~/.config``) adds sixty characters before the test's
    own name. Every daemon then dies with ``socket-path-too-long`` while the product is fine, so the
    suite reports on the machine it ran on rather than on the code -- green on one host, red on
    another, for the same commit.

    So the directory is made here, under a root that is short by contract, and the limit is checked
    before the test body runs. A host without a usable ``/tmp`` falls back to ``tmp_path``; if that
    is still too long the fixture raises rather than skipping, because a path that quietly got
    shorter would let the same test claim success on a machine where the daemon could not start.
    """

    root = Path("/tmp")
    if not (root.is_dir() and os.access(root, os.W_OK | os.X_OK)):
        root = request.getfixturevalue("tmp_path")
    directory = Path(tempfile.mkdtemp(prefix="exd-", dir=str(root)))
    socket_path = directory.joinpath(*_EXEC_SOCKET_SUFFIX)
    if len(str(socket_path)) > EXEC_SOCKET_PATH_LIMIT:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(
            f"short_path cannot hold an execd socket: {socket_path} is "
            f"{len(str(socket_path))} characters, over the daemon's limit of "
            f"{EXEC_SOCKET_PATH_LIMIT} (TMPDIR={os.environ.get('TMPDIR', '<unset>')!r}). "
            "Make a short writable temp root available; do not raise the daemon's limit."
        )
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def stub_web_session(monkeypatch: pytest.MonkeyPatch):
    """Stub the CAS web-session auth path that every CLI command relies on.

    ``config check`` and friends import these lazily, so patching the module
    attributes is enough to keep auth probes out of the network.
    """

    def _stub(
        *,
        ok: bool = True,
        identity: str = "tester",
        error: str = "CAS login returned 401",
    ) -> None:
        browser_api_module = import_module("inspire.platform.web.browser_api")
        web_session_module = import_module("inspire.platform.web.session")

        if ok:

            def _session(**kwargs):  # noqa: ANN003
                from types import SimpleNamespace

                return SimpleNamespace(workspace_id="ws-1")

            monkeypatch.setattr(web_session_module, "get_web_session", _session)
            monkeypatch.setattr(
                browser_api_module,
                "get_current_user",
                lambda session=None: {"username": identity},
            )
        else:

            def _boom(**kwargs):  # noqa: ANN003
                raise RuntimeError(error)

            monkeypatch.setattr(web_session_module, "get_web_session", _boom)

    return _stub


@pytest.fixture
def isolated_cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give a test its own HOME plus a credential-free global config.

    Needed by tests that assert on the exact string handed to a transport. Verbs that prepend the
    resolved ``[remote_env]``/``[remote_runtime]`` read the developer's own configuration, and that
    makes such an assertion pass or fail according to whatever happens to be in the checkout - the
    class of disagreement between a laptop and CI this repo has already been burned by. The fake
    ``[auth]`` block satisfies the configuration guard without touching real credentials.
    """

    home = tmp_path / "cli-home"
    home.mkdir(parents=True, exist_ok=True)
    global_config = home / "config.toml"
    global_config.write_text(
        '[auth]\nusername = "fixture-user"\npassword = "fixture-password"\n', encoding="utf-8"
    )
    from inspire.config import Config

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("INSPIRE_GLOBAL_CONFIG_PATH", str(global_config))
    monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config, raising=False)
    return home


@pytest.fixture(autouse=True)
def isolate_project_config_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep project config discovery inside each test's temp tree.

    Many CLI tests intentionally chdir into pytest temp directories. The real
    config discovery walks upward, which is correct in production but lets a
    host-level /tmp/.inspire/config.toml contaminate unrelated tests. Preserve
    upward search within the test tree while stopping before shared temp roots.

    Ambient ``INSPIRE_*`` / ``INSP_*`` variables are dropped first because env
    has the highest config precedence: a host-exported ``INSPIRE_WORKSPACE_ID``
    silently satisfies workspace selection and turns "missing config" tests into
    live-network calls.
    """

    for name in list(os.environ):
        if name.startswith(ENV_OVERRIDE_PREFIXES) and not name.startswith(ENV_OVERRIDE_KEEP):
            monkeypatch.delenv(name, raising=False)

    stop_dirs = {
        tmp_path.parent.resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    repo_root = Path(__file__).resolve().parents[1]

    from inspire.config import Config

    monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "global.toml")
    monkeypatch.setenv("INSPIRE_STATE_DIR", str(tmp_path / "state"))

    def find_project_config() -> Path | None:
        from inspire.config.models import CONFIG_FILENAME, PROJECT_CONFIG_DIR

        current = Path.cwd().resolve()
        try:
            current.relative_to(repo_root)
            return None
        except ValueError:
            pass
        while True:
            if current in stop_dirs or current == current.parent:
                return None
            config_path = current / PROJECT_CONFIG_DIR / CONFIG_FILENAME
            if config_path.exists():
                return config_path
            current = current.parent

    import inspire.config.load as load_module
    import inspire.config.load_layers as load_layers_module
    import inspire.config.toml as toml_module

    config_set_module = import_module("inspire.cli.commands.config.set_cmd")
    project_select_module = import_module("inspire.cli.commands.project.select")
    runtime_cmd_module = import_module("inspire.cli.commands.runtime.runtime_cmd")
    project_order_module = import_module("inspire.cli.utils.project_order")
    workload_profile_module = import_module("inspire.cli.commands.workload_profile")

    patched_modules = (
        load_module,
        load_layers_module,
        toml_module,
        config_set_module,
        project_select_module,
        runtime_cmd_module,
        project_order_module,
        workload_profile_module,
    )
    for module in patched_modules:
        monkeypatch.setattr(module, "_find_project_config", find_project_config)
    # `import inspire.config.toml as ...` above binds the parent-package attribute, but the
    # loader can also resolve through sys.modules - and the rtunnel-defaults tests pop and
    # re-import inspire.config.* submodules, after which those two names can hold different
    # module objects. Patch whichever object sys.modules carries too, or one unpatched copy
    # leaks the developer's own .inspire/config.toml into unrelated tests.
    for name in (
        "inspire.config.load",
        "inspire.config.load_layers",
        "inspire.config.toml",
    ):
        module = sys.modules.get(name)
        if module is not None and module not in patched_modules:
            monkeypatch.setattr(module, "_find_project_config", find_project_config)
