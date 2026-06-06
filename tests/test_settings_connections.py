from types import SimpleNamespace

from api_gateway.routes.settings_routes import (
    _access_type_for_source,
    _format_source,
    _health_status_from_result,
)


def source(**overrides):
    data = {
        "source_id": "apache-spark-jira",
        "display_name": "Apache Spark",
        "system_type": "jira",
        "base_url": "https://issues.apache.org/jira",
        "port": None,
        "auth_type": "none",
        "auth_secret_ref": "",
        "project_key": "SPARK",
        "ticket_prefix": "SPARK",
        "enabled": True,
        "created_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_access_type_public_for_no_auth_public_endpoint():
    assert _access_type_for_source(source(), token_present=False) == "Public"


def test_access_type_internal_for_private_endpoint():
    internal = source(
        source_id="customer-portal",
        display_name="Customer Portal",
        system_type="customer_portal",
        base_url="http://localhost:8000/mock/customer-portal",
    )
    assert _access_type_for_source(internal, token_present=False) == "Internal"


def test_access_type_authenticated_for_auth_config():
    authenticated = source(
        source_id="apache-kafka-github",
        display_name="Apache Kafka",
        system_type="github",
        base_url="https://api.github.com",
        auth_type="pat",
        auth_secret_ref="KAFKA_TOKEN",
    )
    assert _access_type_for_source(authenticated, token_present=False) == "Authenticated"


def test_health_status_maps_connector_health():
    assert _health_status_from_result({"ok": True, "status": "ok"}, True) == "Connected"
    assert _health_status_from_result({"status": "connected"}, True) == "Connected"
    assert _health_status_from_result({"connected": True}, True) == "Connected"
    assert _health_status_from_result({"is_connected": True}, True) == "Connected"
    assert _health_status_from_result({"test_result": {"status": "ok"}}, True) == "Connected"
    assert _health_status_from_result({"status": "timeout"}, True) == "Timeout"
    assert _health_status_from_result({"status": "error"}, True) == "Error"
    assert _health_status_from_result({"ok": True, "status": "ok"}, False) == "Disconnected"
    assert _health_status_from_result(None, True) == ""


def test_format_source_exposes_access_and_health():
    formatted = _format_source(
        source(),
        {"ok": True, "status": "ok", "latency_ms": 12},
    )
    assert formatted["access_type"] == "Public"
    assert formatted["health_status"] == "Connected"
    assert formatted["latency_ms"] == 12
