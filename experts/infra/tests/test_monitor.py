"""Remote connection validation and process lifecycle without external network access."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from infra_expert import monitor
from infra_expert.api import app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(monitor, "controller", monitor.MonitorController())
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(monitor, "Popen", Mock(return_value=process))
    return TestClient(app), process


def test_connect_switch_disconnect_and_stale_heartbeat(client):
    api, process = client
    body = {"repository": "https://github.com/example/react-app", "branch": "main"}
    first = api.post("/monitor/connect", json=body)
    assert first.status_code == 200
    old_id = first.json()["connection_id"]
    beat = api.post("/monitor/heartbeat", json={"connection_id": old_id, "state": "connected"})
    assert beat.json()["state"] == "connected"
    second = api.post("/monitor/connect", json={**body, "branch": "feature/test"})
    assert second.status_code == 200
    process.terminate.assert_called_once()
    assert (
        api.post(
            "/monitor/heartbeat", json={"connection_id": old_id, "state": "connected"}
        ).status_code
        == 409
    )
    assert api.post("/monitor/disconnect").json()["state"] == "disconnected"
    assert process.terminate.call_count == 2


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/repo",
        "https://token@github.com/org/repo",
        "https://evil.example/org/repo",
        "--upload-pack=anything",
    ],
)
def test_connection_rejects_non_github_or_embedded_credentials(client, url):
    api, process = client
    assert api.post("/monitor/connect", json={"repository": url}).status_code == 422
    process.terminate.assert_not_called()
