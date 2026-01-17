import json
import pytest

from inspire.cli.utils import gitea as gitea_module
from inspire.cli.utils.config import Config


class DummyClient:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.calls = []

    def request_json(self, method: str, url: str):  # noqa: ANN001
        self.calls.append(url)
        if "page=1" in url:
            return {"total_count": 25, "workflow_runs": []}
        if "page=2" in url:
            payload = json.dumps({"inputs": {"request_id": self.request_id}})
            return {
                "workflow_runs": [
                    {
                        "event_payload": payload,
                        "status": "success",
                        "conclusion": "success",
                        "id": 42,
                        "html_url": "https://example.test/run/42",
                    }
                ]
            }
        return {"workflow_runs": []}


def test_wait_for_bridge_action_completion_checks_last_page(monkeypatch: pytest.MonkeyPatch):
    request_id = "req-123"
    client = DummyClient(request_id=request_id)

    monkeypatch.setattr(gitea_module, "_get_client", lambda config: client)
    monkeypatch.setattr(gitea_module, "_get_repo", lambda config: "org/repo")
    monkeypatch.setattr(gitea_module, "_get_server_url", lambda config: "https://codeberg.org")
    monkeypatch.setattr(gitea_module.time, "time", lambda: 0)
    monkeypatch.setattr(gitea_module.time, "sleep", lambda *_args, **_kwargs: None)

    config = Config(username="user", password="pass")

    result = gitea_module.wait_for_bridge_action_completion(
        config=config,
        request_id=request_id,
        timeout=10,
    )

    assert result["run_id"] == 42
    assert result["conclusion"] == "success"
    assert any("page=2" in call for call in client.calls)
