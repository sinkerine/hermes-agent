"""Project memory wire contracts against a real loopback HTTP service."""
import httpx
import pytest

from agent.project_memory.backend import BackendError, RecallHit, RetainItem
from agent.project_memory.config import API_KEY_ENV, ProjectMemoryServiceConfig, load_service_config
from agent.project_memory.hindsight import HindsightBackend, backend_for_current_profile
from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.fakes.hindsight_standin import HindsightStandin


@pytest.fixture
def standin():
    with HindsightStandin() as server:
        yield server


@pytest.fixture
def backend(standin):
    client = HindsightBackend(ProjectMemoryServiceConfig(standin.url))
    yield client
    client.close()


def test_bank_lifecycle_wire_and_url_encoding(standin, backend):
    bank = "alpha:team/name space"
    path = "/v1/default/banks/alpha%3Ateam%2Fname%20space"
    assert not backend.bank_exists(bank)
    backend.ensure_bank(bank, name="Alpha")
    backend.ensure_bank(bank, name="Alpha")
    assert backend.bank_exists(bank)
    requests = standin.requests(path)
    assert [r["method"] for r in requests] == ["GET", "PUT", "PUT", "GET"]
    assert requests[0]["path"] == path + "/config"
    assert requests[1]["body"] == {"name": "Alpha"}
    assert all("Authorization" not in r["headers"] for r in requests)


def test_recall_and_retain_wire(standin, backend):
    standin.seed("a", [{"id": "hit1", "text": "A fact", "type": "world",
                        "occurred_start": "2026-01-01T00:00:00Z"}])
    assert backend.recall("a", "find fact") == [RecallHit(
        "a", "A fact", "hit1", "world", "2026-01-01T00:00:00Z")]
    backend.retain("a", [RetainItem("conversation", "project", "doc1", ("hermes",),
                                   {"platform": "matrix"}, "2026-01-02T00:00:00Z"),
                         RetainItem("other", "project", "doc2")])
    recall, retain = standin.requests()
    assert recall["path"] == "/v1/default/banks/a/memories/recall"
    assert recall["body"] == {"query": "find fact", "budget": "mid", "max_tokens": 1024}
    assert retain["method"] == "POST"
    assert retain["path"] == "/v1/default/banks/a/memories"
    assert retain["body"] == {"async": True, "items": [
        {"content": "conversation", "context": "project", "document_id": "doc1",
         "tags": ["hermes"], "metadata": {"platform": "matrix"}, "timestamp": "2026-01-02T00:00:00Z"},
        {"content": "other", "context": "project", "document_id": "doc2", "tags": [], "metadata": {}}]}


def test_missing_recall_and_http_errors(standin, backend):
    assert backend.recall("missing", "query") == []
    standin.seed("a", [])
    for operation in (lambda: backend.recall("a", "q"), lambda: backend.bank_exists("a"),
                      lambda: backend.ensure_bank("a", name="A"),
                      lambda: backend.retain("a", [RetainItem("x", "y", "z")])):
        standin.queue_fault("a", status=503)
        with pytest.raises(BackendError) as error:
            operation()
        assert error.value.status == 503
    standin.queue_fault("a", status=401)
    with pytest.raises(BackendError) as error:
        backend.recall("a", "q")
    assert error.value.status == 401


def test_recall_timeout_is_backend_error(standin):
    standin.seed("a", [])
    standin.queue_fault("a", status=200, delay=5)
    client = HindsightBackend(ProjectMemoryServiceConfig(standin.url, timeout=0.1))
    try:
        with pytest.raises(BackendError) as error:
            client.recall("a", "q")
        assert error.value.status is None
    finally:
        client.close()


def test_custom_recall_settings_and_bearer(standin):
    standin.seed("a", [])
    client = HindsightBackend(ProjectMemoryServiceConfig(standin.url, "high", 73), api_key="private-key")
    try:
        client.recall("a", "q")
    finally:
        client.close()
    request = standin.requests()[0]
    assert request["headers"]["Authorization"] == "Bearer private-key"
    assert request["body"] == {"query": "q", "budget": "high", "max_tokens": 73}


@pytest.mark.parametrize("config", [{}, {"project_memory": {}},
                                       {"project_memory": {"hindsight_url": " "}}])
def test_unconfigured_service(config):
    assert load_service_config(config) is None


@pytest.mark.parametrize("field,value", [
    ("hindsight_url", "ftp://localhost"), ("hindsight_url", "https://"),
    ("hindsight_url", 1), ("recall_budget", "huge"), ("recall_budget", []),
    ("recall_max_tokens", 0), ("recall_max_tokens", -1), ("recall_max_tokens", True),
    ("recall_max_tokens", 1.5), ("timeout", 0), ("timeout", -1),
    ("timeout", "10"), ("timeout", True), ("timeout", float("nan")),
    ("timeout", float("inf")),
])
def test_invalid_service_config(field, value):
    section = {"hindsight_url": "http://localhost:8888", field: value}
    with pytest.raises(ValueError):
        load_service_config({"project_memory": section})


def test_invalid_section():
    with pytest.raises(ValueError):
        load_service_config({"project_memory": "wrong"})


def test_config_yaml_reaches_service_loader(tmp_path):
    from hermes_cli.config import atomic_config_write, load_config

    home_token = set_hermes_home_override(tmp_path)
    try:
        atomic_config_write(tmp_path / "config.yaml", {"project_memory": {
            "hindsight_url": "http://127.0.0.1:8888/base/", "recall_budget": "low",
            "recall_max_tokens": 91, "timeout": 3.5}})
        assert load_service_config(load_config()) == ProjectMemoryServiceConfig(
            "http://127.0.0.1:8888/base", "low", 91, 3.5)
    finally:
        reset_hermes_home_override(home_token)


def test_backend_reads_each_scoped_home_dotenv(tmp_path, standin):
    from hermes_cli.config import OPTIONAL_ENV_VARS

    assert OPTIONAL_ENV_VARS[API_KEY_ENV]["password"] is True
    clients = []
    try:
        for name in ("first", "second"):
            home = tmp_path / name
            home.mkdir()
            (home / ".env").write_text(f"{API_KEY_ENV}={name}-key\n")
            home_token = set_hermes_home_override(home)
            secret_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
            try:
                clients.append(backend_for_current_profile(ProjectMemoryServiceConfig(standin.url)))
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
        for client in clients:
            assert not client.bank_exists("missing")
        assert [r["headers"]["Authorization"] for r in standin.requests()] == [
            "Bearer first-key", "Bearer second-key"]
    finally:
        for client in clients:
            client.close()


def test_unknown_standin_route_is_recorded(standin):
    response = httpx.get(standin.url + "/unknown")
    assert response.status_code == 404
    assert standin.requests("/unknown")[0]["method"] == "GET"
