"""Service tests — auto-skip gracefully when FastAPI is absent."""
import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from awap.service import create_app  # noqa: E402


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(create_app())


def _workflow_payload():
    return {
        "id": "svc-demo",
        "name": "Service Demo",
        "tenant": "acme",
        "steps": [
            {"id": "draft", "type": "llm",
             "config": {"prompt": "welcome", "output_key": "draft"}},
            {"id": "lookup", "type": "tool",
             "config": {"tool": "crm_lookup", "args": {}, "output_key": "record"}},
        ],
    }


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "service": "awap"}


def test_create_and_list_workflow(client):
    resp = client.post("/workflows", json=_workflow_payload())
    assert resp.status_code == 201
    assert resp.json()["version"] == 1
    listed = client.get("/workflows").json()
    assert any(w["id"] == "svc-demo" for w in listed)


def test_invalid_workflow_rejected(client):
    payload = _workflow_payload()
    payload["steps"] = [{"id": "x", "type": "llm", "config": {}}]
    resp = client.post("/workflows", json=payload)
    assert resp.status_code == 422


def test_run_workflow_end_to_end(client):
    client.post("/workflows", json=_workflow_payload())
    resp = client.post("/workflows/svc-demo/run", json={"input_data": {}})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "succeeded"
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["tokens"] > 0
    trace = client.get(f"/runs/{run_id}/trace").json()
    assert [t["step_id"] for t in trace] == ["draft", "lookup"]


def test_run_unknown_workflow_404(client):
    assert client.post("/workflows/nope/run", json={}).status_code == 404


def test_approval_flow_over_http(client):
    payload = _workflow_payload()
    payload["id"] = "svc-approval"
    payload["steps"].append(
        {"id": "review", "type": "approval",
         "config": {"approvers": ["ops-lead"], "expires_in_s": 3600,
                    "on_expiry": "escalate", "escalate_to": ["duty-manager"]}})
    client.post("/workflows", json=payload)
    run = client.post("/workflows/svc-approval/run", json={}).json()
    assert run["status"] == "waiting_approval"
    resp = client.post(f"/runs/{run['run_id']}/approve/review",
                       json={"approver": "ops-lead", "decision": "approve"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


def test_rollback_over_http(client):
    client.post("/workflows", json=_workflow_payload())
    resp = client.post("/workflows/svc-demo/rollback/1")
    assert resp.status_code == 201
    assert resp.json()["version"] == 2
    assert resp.json()["restored_from"] == 1


def test_audit_verify_endpoint(client):
    resp = client.get("/audit/verify").json()
    assert resp["ok"] is True
