import asyncio
import sys
import types
from dataclasses import dataclass

from orchestrator.connectors import registry
from orchestrator.connectors.base_connector import BaseConnector
from orchestrator.connectors.support_kb_connector import SupportKBConnector


@dataclass
class SourceRow:
    source_id: str
    system_type: str
    ticket_prefix: str = ""
    enabled: bool = True
    display_name: str = ""
    base_url: str = "https://example.test"
    project_key: str = ""
    auth_type: str = "bearer_token"
    auth_secret_ref: str = ""
    port: int | None = None


class FakeConnector(BaseConnector):
    async def get(self, ticket_id: str):
        return None

    async def search(self, query: str, max_results: int = 10):
        return []

    async def get_linked_items(self, ticket_id: str):
        return []

    async def get_lightweight(self, ticket_id: str):
        return {}

    def extract_links(self, raw_payload: dict):
        return []

    async def health_check(self):
        return {
            "source_id": self.source_id,
            "system_type": self.system_type,
            "status": "ok",
            "ok": True,
            "latency_ms": 1,
            "error": "",
        }


class FailingHealthConnector(FakeConnector):
    async def health_check(self):
        raise RuntimeError("health exploded")


class BrokenInitConnector(FakeConnector):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("init exploded")


class FakeTokenProvider:
    def get_token(self, secret_ref: str | None) -> str:
        return "fake-token" if secret_ref else ""


class FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def run(coro):
    return asyncio.run(coro)


def install_fake_source_registry(monkeypatch, rows):
    session_module = types.ModuleType("orchestrator.db.session")
    session_module.AsyncSessionLocal = lambda: FakeSession()

    repository_module = types.ModuleType(
        "orchestrator.db.repositories.source_registry"
    )

    async def get_enabled_sources(_db):
        return [row for row in rows if row.enabled]

    repository_module.get_enabled_sources = get_enabled_sources

    monkeypatch.setitem(sys.modules, "orchestrator.db.session", session_module)
    monkeypatch.setitem(
        sys.modules,
        "orchestrator.db.repositories.source_registry",
        repository_module,
    )


def install_fake_connectors(monkeypatch, mapping=None):
    fake_mapping = {
        "jira": FakeConnector,
        "github": FakeConnector,
        "bugzilla": FakeConnector,
        "confluence": FakeConnector,
        "customer_portal": FakeConnector,
        "support_kb": FakeConnector,
    }
    if mapping:
        fake_mapping.update(mapping)
    monkeypatch.setattr(registry, "SYSTEM_TYPE_TO_CLASS", fake_mapping)
    registry.set_token_provider(FakeTokenProvider())


def test_get_all_enabled_returns_only_enabled_connectors(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-storage", "jira", "STO", enabled=True),
        SourceRow("github-disabled", "github", "GH", enabled=False),
        SourceRow("bugzilla-fw", "bugzilla", "BZ", enabled=True),
    ])
    install_fake_connectors(monkeypatch)

    connectors = run(registry.ConnectorRegistry.get_all_enabled())

    assert [connector.source_id for connector in connectors] == [
        "jira-storage",
        "bugzilla-fw",
    ]


def test_disabled_connectors_are_skipped(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-disabled", "jira", "STO", enabled=False),
    ])
    install_fake_connectors(monkeypatch)

    connectors = run(registry.ConnectorRegistry.get_all_enabled())

    assert connectors == []


def test_broken_connector_initialization_does_not_crash_registry(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-storage", "jira", "STO", enabled=True),
        SourceRow("broken-source", "broken", "BAD", enabled=True),
        SourceRow("github-hpe", "github", "GH", enabled=True),
    ])
    install_fake_connectors(monkeypatch, {
        "broken": BrokenInitConnector,
    })

    connectors = run(registry.ConnectorRegistry.get_all_enabled())

    assert [connector.source_id for connector in connectors] == [
        "jira-storage",
        "github-hpe",
    ]


def test_get_returns_correct_connector(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-storage", "jira", "STO", enabled=True),
        SourceRow("github-hpe", "github", "GH", enabled=True),
    ])
    install_fake_connectors(monkeypatch)

    connector = run(registry.ConnectorRegistry.get("github-hpe"))

    assert connector is not None
    assert connector.source_id == "github-hpe"
    assert connector.system_type == "github"


def test_get_by_ticket_id_resolves_by_ticket_prefix(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-network", "jira", "NET", enabled=True),
        SourceRow("jira-storage", "jira", "STO", enabled=True),
    ])
    install_fake_connectors(monkeypatch)

    connector = run(registry.ConnectorRegistry.get_by_ticket_id("STO-1089"))

    assert connector is not None
    assert connector.source_id == "jira-storage"


def test_support_kb_is_mapped_as_first_class_connector_type():
    assert registry.SYSTEM_TYPE_TO_CLASS["support_kb"] is SupportKBConnector
    assert registry.get_connector_class("support_kb") is SupportKBConnector


def test_health_check_all_returns_status_per_enabled_connector(monkeypatch):
    install_fake_source_registry(monkeypatch, [
        SourceRow("jira-storage", "jira", "STO", enabled=True),
        SourceRow("github-hpe", "github", "GH", enabled=True),
        SourceRow("portal-disabled", "customer_portal", "CASE", enabled=False),
    ])
    install_fake_connectors(monkeypatch, {
        "github": FailingHealthConnector,
    })

    statuses = run(registry.ConnectorRegistry.health_check_all(timeout=1))

    assert [status["source_id"] for status in statuses] == [
        "jira-storage",
        "github-hpe",
    ]
    assert statuses[0]["status"] == "ok"
    assert statuses[0]["ok"] is True
    assert statuses[1]["status"] == "error"
    assert statuses[1]["ok"] is False
    assert "health exploded" in statuses[1]["error"]
