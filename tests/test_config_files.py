"""Tests for TOML config file loading and layered configuration."""

import json
import os
from pathlib import Path
from typing import Generator

import pytest
from click.testing import CliRunner

from inspire.cli.utils.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    SOURCE_ENV,
    PROJECT_CONFIG_DIR,
    CONFIG_FILENAME,
)
from inspire.cli.utils.config_schema import (
    CONFIG_OPTIONS,
    ConfigOption,
    get_categories,
    get_options_by_category,
    get_options_by_scope,
    get_option_by_env,
    get_option_by_toml,
)
from inspire.cli.commands.init import init, _detect_env_vars, _generate_toml_content
from inspire.cli.commands.config import config as config_command


# ===========================================================================
# Config Schema tests
# ===========================================================================


class TestConfigSchema:
    """Tests for config schema module."""

    def test_config_options_not_empty(self) -> None:
        """Test that CONFIG_OPTIONS has entries."""
        assert len(CONFIG_OPTIONS) > 0

    def test_all_options_have_required_fields(self) -> None:
        """Test that all options have required fields."""
        for opt in CONFIG_OPTIONS:
            assert opt.env_var, f"Option missing env_var: {opt}"
            assert opt.toml_key, f"Option missing toml_key: {opt}"
            assert opt.description, f"Option missing description: {opt}"
            assert opt.category, f"Option missing category: {opt}"

    def test_get_option_by_env(self) -> None:
        """Test getting option by env var."""
        opt = get_option_by_env("INSPIRE_USERNAME")
        assert opt is not None
        assert opt.toml_key == "auth.username"

    def test_get_option_by_toml(self) -> None:
        """Test getting option by TOML key."""
        opt = get_option_by_toml("auth.username")
        assert opt is not None
        assert opt.env_var == "INSPIRE_USERNAME"

    def test_get_option_not_found(self) -> None:
        """Test getting non-existent option."""
        assert get_option_by_env("NONEXISTENT_VAR") is None
        assert get_option_by_toml("nonexistent.key") is None

    def test_get_categories(self) -> None:
        """Test getting all categories."""
        categories = get_categories()
        assert len(categories) > 0
        assert "Authentication" in categories
        assert "API" in categories

    def test_get_options_by_category(self) -> None:
        """Test getting options by category."""
        auth_opts = get_options_by_category("Authentication")
        assert len(auth_opts) >= 2  # username and password
        for opt in auth_opts:
            assert opt.category == "Authentication"

    def test_scope_field_on_config_option(self) -> None:
        """Test that ConfigOption has scope field with valid values."""
        for opt in CONFIG_OPTIONS:
            assert hasattr(opt, "scope"), f"Option {opt.env_var} missing scope field"
            assert opt.scope in ("global", "project"), f"Option {opt.env_var} has invalid scope: {opt.scope}"

    def test_global_scope_options(self) -> None:
        """Test that expected options have global scope."""
        global_opts = get_options_by_scope("global")
        global_env_vars = [opt.env_var for opt in global_opts]

        # Auth should be global
        assert "INSPIRE_USERNAME" in global_env_vars
        assert "INSPIRE_PASSWORD" in global_env_vars

        # API settings should be global
        assert "INSPIRE_BASE_URL" in global_env_vars
        assert "INSPIRE_TIMEOUT" in global_env_vars

        # Gitea server and token should be global
        assert "INSP_GITEA_SERVER" in global_env_vars
        assert "INSP_GITEA_TOKEN" in global_env_vars

        # SSH paths should be global
        assert "INSPIRE_RTUNNEL_BIN" in global_env_vars

        # Mirrors should be global
        assert "INSPIRE_APT_MIRROR_URL" in global_env_vars

    def test_project_scope_options(self) -> None:
        """Test that expected options have project scope."""
        project_opts = get_options_by_scope("project")
        project_env_vars = [opt.env_var for opt in project_opts]

        # Paths like target_dir should be project
        assert "INSPIRE_TARGET_DIR" in project_env_vars
        assert "INSPIRE_LOG_PATTERN" in project_env_vars

        # Gitea repo should be project
        assert "INSP_GITEA_REPO" in project_env_vars

        # Job/Notebook settings should be project
        assert "INSP_PRIORITY" in project_env_vars
        assert "INSPIRE_NOTEBOOK_RESOURCE" in project_env_vars

        # Bridge/Sync settings should be project
        assert "INSPIRE_BRIDGE_DENYLIST" in project_env_vars
        assert "INSPIRE_DEFAULT_REMOTE" in project_env_vars

    def test_get_options_by_scope(self) -> None:
        """Test get_options_by_scope helper function."""
        global_opts = get_options_by_scope("global")
        project_opts = get_options_by_scope("project")

        assert len(global_opts) > 0
        assert len(project_opts) > 0

        # All returned options should have correct scope
        for opt in global_opts:
            assert opt.scope == "global"
        for opt in project_opts:
            assert opt.scope == "project"

        # Together they should cover all options
        assert len(global_opts) + len(project_opts) == len(CONFIG_OPTIONS)


# ===========================================================================
# TOML loading tests
# ===========================================================================


class TestTomlLoading:
    """Tests for TOML config file loading."""

    def test_load_toml_basic(self, tmp_path: Path) -> None:
        """Test loading a basic TOML file."""
        toml_content = """
[auth]
username = "tomluser"

[api]
base_url = "https://custom.example.com"
timeout = 60
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        data = Config._load_toml(config_file)
        assert data["auth"]["username"] == "tomluser"
        assert data["api"]["base_url"] == "https://custom.example.com"
        assert data["api"]["timeout"] == 60

    def test_flatten_toml(self) -> None:
        """Test flattening nested TOML structure."""
        data = {
            "auth": {"username": "test", "password": "secret"},
            "api": {"base_url": "https://example.com"},
        }

        flat = Config._flatten_toml(data)

        assert flat["auth.username"] == "test"
        assert flat["auth.password"] == "secret"
        assert flat["api.base_url"] == "https://example.com"

    def test_toml_key_to_field(self) -> None:
        """Test mapping TOML keys to Config field names."""
        assert Config._toml_key_to_field("auth.username") == "username"
        assert Config._toml_key_to_field("api.timeout") == "timeout"
        assert Config._toml_key_to_field("paths.target_dir") == "target_dir"
        assert Config._toml_key_to_field("nonexistent.key") is None


# ===========================================================================
# Layered config tests
# ===========================================================================


class TestLayeredConfig:
    """Tests for layered configuration loading."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        env_vars = [
            "INSPIRE_USERNAME", "INSPIRE_PASSWORD", "INSPIRE_BASE_URL",
            "INSPIRE_TIMEOUT", "INSPIRE_TARGET_DIR", "INSP_GITEA_SERVER",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)
        yield

    def test_from_files_and_env_defaults_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test config with only defaults (no files, no env)."""
        # Point to non-existent config paths
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.base_url == "https://api.example.com"
        assert cfg.timeout == 30
        assert sources["base_url"] == SOURCE_DEFAULT
        assert sources["timeout"] == SOURCE_DEFAULT

    def test_from_files_and_env_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading values from global config."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text("""
[auth]
username = "globaluser"

[api]
base_url = "https://global.example.com"
timeout = 45
""")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "globaluser"
        assert cfg.base_url == "https://global.example.com"
        assert cfg.timeout == 45
        assert sources["username"] == SOURCE_GLOBAL
        assert sources["base_url"] == SOURCE_GLOBAL

    def test_from_files_and_env_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading values from project config."""
        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text("""
[auth]
username = "projectuser"

[api]
timeout = 120
""")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.username == "projectuser"
        assert cfg.timeout == 120
        assert sources["username"] == SOURCE_PROJECT
        assert sources["timeout"] == SOURCE_PROJECT

    def test_from_files_and_env_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that project config overrides global config."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text("""
[auth]
username = "globaluser"

[api]
timeout = 45
base_url = "https://global.example.com"
""")

        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text("""
[api]
timeout = 120
""")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Username from global
        assert cfg.username == "globaluser"
        assert sources["username"] == SOURCE_GLOBAL

        # base_url from global
        assert cfg.base_url == "https://global.example.com"
        assert sources["base_url"] == SOURCE_GLOBAL

        # timeout overridden by project
        assert cfg.timeout == 120
        assert sources["timeout"] == SOURCE_PROJECT

    def test_from_files_and_env_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that env vars override config files."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text("""
[auth]
username = "globaluser"

[api]
timeout = 45
""")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "envuser")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "90")

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Env vars should override
        assert cfg.username == "envuser"
        assert cfg.timeout == 90
        assert sources["username"] == SOURCE_ENV
        assert sources["timeout"] == SOURCE_ENV

    def test_from_files_and_env_remote_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test loading remote_env section from config files."""
        # Create global config with remote_env
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text('''
[auth]
username = "testuser"

[remote_env]
WANDB_API_KEY = "global-key"
UV_PYTHON_INSTALL_DIR = "/path/to/uv"
''')

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        assert cfg.remote_env == {
            "WANDB_API_KEY": "global-key",
            "UV_PYTHON_INSTALL_DIR": "/path/to/uv",
        }
        assert sources["remote_env"] == SOURCE_GLOBAL

    def test_from_files_and_env_remote_env_project_merges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that project remote_env merges with global."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text('''
[remote_env]
WANDB_API_KEY = "global-key"
UV_PYTHON_INSTALL_DIR = "/path/to/uv"
''')

        # Create project config with different remote_env
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text('''
[remote_env]
WANDB_API_KEY = "project-key"
HF_TOKEN = "hf-token"
''')

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        cfg, sources = Config.from_files_and_env(require_credentials=False)

        # Project should override WANDB_API_KEY and add HF_TOKEN
        # UV_PYTHON_INSTALL_DIR from global should remain
        assert cfg.remote_env == {
            "WANDB_API_KEY": "project-key",
            "UV_PYTHON_INSTALL_DIR": "/path/to/uv",
            "HF_TOKEN": "hf-token",
        }
        assert sources["remote_env"] == SOURCE_PROJECT

    def test_find_project_config_walks_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that project config search walks up directories."""
        # Create project structure: tmp/inspire/config.toml
        inspire_dir = tmp_path / ".inspire"
        inspire_dir.mkdir()
        config_file = inspire_dir / "config.toml"
        config_file.write_text("[api]\ntimeout = 77")

        # Work from a subdirectory: tmp/subdir/deep
        subdir = tmp_path / "subdir" / "deep"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        found = Config._find_project_config()

        assert found == config_file

    def test_from_files_and_env_require_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test error when credentials required but missing."""
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError, match="Missing username"):
            Config.from_files_and_env(require_credentials=True)

    def test_get_config_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test get_config_paths returns correct paths."""
        # Create global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_config = global_dir / "config.toml"
        global_config.write_text("[api]\ntimeout = 1")

        # Create project config
        project_dir = tmp_path / ".inspire"
        project_dir.mkdir()
        project_config = project_dir / "config.toml"
        project_config.write_text("[api]\ntimeout = 2")

        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        global_path, project_path = Config.get_config_paths()

        assert global_path == global_config
        assert project_path == project_config


# ===========================================================================
# Init command tests
# ===========================================================================


class TestInitCommand:
    """Tests for inspire init command."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        # Clear all INSPIRE_* and INSP_* env vars
        for key in list(os.environ.keys()):
            if key.startswith("INSPIRE_") or key.startswith("INSP_"):
                monkeypatch.delenv(key, raising=False)
        yield

    def test_init_creates_template_when_no_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init creates template config when no env vars detected."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        # Simulate choosing project config
        result = runner.invoke(init, input="p\n")

        assert result.exit_code == 0
        assert "No environment variables detected" in result.output
        config_file = tmp_path / ".inspire" / "config.toml"
        assert config_file.exists()
        content = config_file.read_text()
        assert "[auth]" in content
        assert "[api]" in content
        assert "your_username" in content  # Template placeholder

    def test_init_template_flag_creates_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that --template flag creates template even with env vars."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--project"])

        assert result.exit_code == 0
        assert "Creating template config" in result.output
        config_file = tmp_path / ".inspire" / "config.toml"
        assert config_file.exists()
        content = config_file.read_text()
        # Should have template placeholder, not actual env var value
        assert "your_username" in content
        assert "testuser" not in content

    def test_init_global_creates_global_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init --global creates global config."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--template"])

        assert result.exit_code == 0
        assert global_config.exists()
        content = global_config.read_text()
        assert "[auth]" in content

    def test_init_warns_on_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init warns when config exists."""
        monkeypatch.chdir(tmp_path)

        # Create existing config
        config_dir = tmp_path / ".inspire"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        # Simulate choosing 'p' then declining overwrite
        result = runner.invoke(init, input="p\nn\n")

        assert "already exists" in result.output
        assert "Aborted" in result.output
        # Original should be unchanged
        assert "existing" in config_file.read_text()

    def test_init_force_overwrites_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --force overwrites existing config without prompting."""
        monkeypatch.chdir(tmp_path)

        # Create existing config
        config_dir = tmp_path / ".inspire"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text("[auth]\nusername = 'existing'")

        runner = CliRunner()
        result = runner.invoke(init, ["--template", "--project", "--force"])

        assert result.exit_code == 0
        content = config_file.read_text()
        assert "existing" not in content
        assert "your_username" in content

    def test_init_with_env_vars_auto_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test init with env vars uses auto-split by scope."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Both files should exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert global_config.exists(), "Global config should be created"
        assert project_config.exists(), "Project config should be created"

        # Global config should have username only
        global_content = global_config.read_text()
        assert 'username = "testuser"' in global_content
        assert "target_dir" not in global_content

        # Project config should have target_dir only
        project_content = project_config.read_text()
        assert 'target_dir = "/shared/myproject"' in project_content
        assert "username" not in project_content

    def test_init_global_flag_forces_all_to_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --global forces all options to global config."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--force"])

        assert result.exit_code == 0
        assert global_config.exists()

        # Global config should have BOTH values
        global_content = global_config.read_text()
        assert 'username = "testuser"' in global_content
        assert 'target_dir = "/shared/myproject"' in global_content

        # Project config should NOT exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert not project_config.exists()

    def test_init_project_flag_forces_all_to_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --project forces all options to project config."""
        monkeypatch.chdir(tmp_path)

        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        runner = CliRunner()
        result = runner.invoke(init, ["--project", "--force"])

        assert result.exit_code == 0

        # Project config should have BOTH values
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'username = "testuser"' in project_content
        assert 'target_dir = "/shared/myproject"' in project_content

    def test_init_excludes_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that init excludes secrets from config files."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "secretpass")

        runner = CliRunner()
        result = runner.invoke(init, ["--project", "--force"])

        assert result.exit_code == 0
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        content = project_config.read_text()

        # Username should be written
        assert 'username = "testuser"' in content
        # Password should be excluded (commented)
        assert "secretpass" not in content
        assert "# password - use env var INSPIRE_PASSWORD for security" in content

    def test_init_both_flags_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that --global and --project together is an error."""
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(init, ["--global", "--project"])

        assert result.exit_code != 0
        assert "Cannot specify both" in result.output

    def test_init_auto_split_only_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only global-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set only global scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Global config should exist
        assert global_config.exists()
        global_content = global_config.read_text()
        assert 'username = "testuser"' in global_content
        assert 'base_url = "https://custom.example.com"' in global_content

        # Project config should NOT exist (no project-scope vars)
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert not project_config.exists()

    def test_init_auto_split_only_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test auto-split with only project-scope env vars."""
        global_config = tmp_path / ".config" / "inspire" / "config.toml"
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", global_config)
        monkeypatch.chdir(tmp_path)

        # Set only project scope env vars
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")
        monkeypatch.setenv("INSP_GITEA_REPO", "user/repo")

        runner = CliRunner()
        result = runner.invoke(init, ["--force"])

        assert result.exit_code == 0

        # Project config should exist
        project_config = tmp_path / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        assert project_config.exists()
        project_content = project_config.read_text()
        assert 'target_dir = "/shared/myproject"' in project_content
        assert 'repo = "user/repo"' in project_content

        # Global config should NOT exist (no global-scope vars)
        assert not global_config.exists()


# ===========================================================================
# Init helper function tests
# ===========================================================================


class TestInitHelpers:
    """Tests for init command helper functions."""

    @pytest.fixture
    def clean_env(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
        """Clear relevant env vars for testing."""
        for key in list(os.environ.keys()):
            if key.startswith("INSPIRE_") or key.startswith("INSP_"):
                monkeypatch.delenv(key, raising=False)
        yield

    def test_detect_env_vars(self, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
        """Test detecting set environment variables."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")

        detected = _detect_env_vars()

        env_vars = [opt.env_var for opt, _ in detected]
        assert "INSPIRE_USERNAME" in env_vars
        assert "INSPIRE_BASE_URL" in env_vars

    def test_detect_env_vars_empty(self, clean_env: None) -> None:
        """Test detecting no set environment variables."""
        detected = _detect_env_vars()
        assert len(detected) == 0

    def test_generate_toml_content(self, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
        """Test TOML content generation."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_BASE_URL", "https://custom.example.com")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "60")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert "[auth]" in toml_content
        assert 'username = "testuser"' in toml_content
        assert "[api]" in toml_content
        assert 'base_url = "https://custom.example.com"' in toml_content
        assert "timeout = 60" in toml_content

    def test_generate_toml_excludes_secrets(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that secrets are always excluded."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "secretpass")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert 'username = "testuser"' in toml_content
        # Password should be commented out
        assert "# password - use env var INSPIRE_PASSWORD for security" in toml_content
        assert 'password = "secretpass"' not in toml_content

    def test_generate_toml_content_with_scope_filter(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test _generate_toml_content with scope_filter parameter."""
        # Set both global and project scope env vars
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")  # global
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/myproject")  # project

        detected = _detect_env_vars()

        # Generate with global filter
        global_content = _generate_toml_content(detected, scope_filter="global")
        assert 'username = "testuser"' in global_content
        assert "target_dir" not in global_content

        # Generate with project filter
        project_content = _generate_toml_content(detected, scope_filter="project")
        assert "username" not in project_content
        assert 'target_dir = "/shared/myproject"' in project_content

        # Generate without filter (all options)
        all_content = _generate_toml_content(detected)
        assert 'username = "testuser"' in all_content
        assert 'target_dir = "/shared/myproject"' in all_content

    def test_generate_toml_list_values(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test TOML generation with list values."""
        monkeypatch.setenv("INSPIRE_BRIDGE_DENYLIST", "*.pyc,__pycache__,*.log")

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        assert "[bridge]" in toml_content
        assert 'denylist = ["*.pyc", "__pycache__", "*.log"]' in toml_content

    def test_generate_toml_preserves_special_chars(
        self, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        """Test that special characters in values are properly escaped."""
        monkeypatch.setenv("INSPIRE_BASE_URL", 'https://example.com/path?foo=bar&baz="test"')

        detected = _detect_env_vars()
        toml_content = _generate_toml_content(detected)

        # Value should be properly escaped
        assert 'base_url = "https://example.com/path?foo=bar&baz=\\"test\\""' in toml_content


# ===========================================================================
# Config show command tests
# ===========================================================================


class TestConfigShowCommand:
    """Tests for inspire config show command."""

    def test_config_show_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test config show table output."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show"])

        assert result.exit_code == 0
        assert "Configuration Overview" in result.output
        assert "INSPIRE_USERNAME" in result.output
        assert "testuser" in result.output
        assert "[env]" in result.output

    def test_config_show_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test config show JSON output."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--format", "json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "config_files" in data
        assert "values" in data
        assert "INSPIRE_USERNAME" in data["values"]

    def test_config_show_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test config show with category filter."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setattr(Config, "GLOBAL_CONFIG_PATH", tmp_path / "nonexistent" / "config.toml")
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(config_command, ["show", "--filter", "auth"])

        assert result.exit_code == 0
        assert "Authentication" in result.output
        # Other categories should not appear
        assert "Gitea" not in result.output


# ===========================================================================
# Config env command tests
# ===========================================================================


class TestConfigEnvCommand:
    """Tests for inspire config env command."""

    def test_config_env_minimal(self) -> None:
        """Test config env minimal template."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["env"])

        assert result.exit_code == 0
        assert "# Inspire CLI Environment Variables" in result.output
        assert "INSPIRE_USERNAME" in result.output
        # Minimal should include essential categories
        assert "=== Authentication ===" in result.output
        assert "=== API ===" in result.output

    def test_config_env_full(self) -> None:
        """Test config env full template."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["env", "--template", "full"])

        assert result.exit_code == 0
        # Full template should include all categories
        assert "=== Job ===" in result.output
        assert "=== Notebook ===" in result.output

    def test_config_env_output_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config env writing to file."""
        monkeypatch.chdir(tmp_path)
        output_file = tmp_path / ".env.example"

        runner = CliRunner()
        result = runner.invoke(config_command, ["env", "--output", str(output_file)])

        assert result.exit_code == 0
        assert output_file.exists()
        content = output_file.read_text()
        assert "INSPIRE_USERNAME" in content


# ===========================================================================
# Migrate command removed - verify it no longer exists
# ===========================================================================


class TestMigrateCommandRemoved:
    """Tests to verify migrate command has been removed."""

    def test_migrate_command_does_not_exist(self) -> None:
        """Test that 'inspire config migrate' is no longer a valid command."""
        runner = CliRunner()
        result = runner.invoke(config_command, ["migrate"])

        # Should fail with "No such command"
        assert result.exit_code != 0
        assert "No such command" in result.output or "Error" in result.output
