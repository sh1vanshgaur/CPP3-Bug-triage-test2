import asyncio

from orchestrator.agents import cross_system_fetch as cross_module
from orchestrator.agents.cross_system_fetch import CrossSystemFetchAgent


def run(coro):
    return asyncio.run(coro)


class EmptyRegistry:
    @classmethod
    async def get_all_enabled(cls):
        return []


class CacheOnlyCrossSystemFetchAgent(CrossSystemFetchAgent):
    def __init__(self, candidates):
        self.candidates = candidates

    async def _scan_redis_cache(self, keywords):
        return list(self.candidates)

    async def _live_api_search(
            self,
            primary,
            primary_source,
            context,
            all_connectors=None):
        return [], []


def primary_context():
    return {
        "source_id": "jira-storage",
        "primary_ticket": {
            "ticket_id": "STO-1089",
            "id": "STO-1089",
            "source_id": "jira-storage",
            "system_type": "jira",
            "title": "StorageController NPE during VM provisioning",
            "description": "NullPointerException in allocate",
            "component": "StorageController",
            "comments": [],
        },
    }


def test_cross_system_excludes_primary_ticket(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(cross_module, "ConnectorRegistry", EmptyRegistry)
    agent = CacheOnlyCrossSystemFetchAgent([
        {
            "id": "STO-1089",
            "ticket_id": "STO-1089",
            "title": "StorageController NPE during VM provisioning",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 9,
        },
        {
            "id": "REL-1",
            "ticket_id": "REL-1",
            "title": "Similar allocation NPE",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 8,
        },
    ])

    context = run(agent.run(primary_context()))

    ids = [item["ticket_id"] for item in context["related_tickets"]]
    assert "STO-1089" not in ids
    assert ids == ["REL-1"]
    assert context["related_issues"]["related_tickets"] == context["related_tickets"]


def test_cross_system_deduplicates_repeated_related_tickets(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(cross_module, "ConnectorRegistry", EmptyRegistry)
    agent = CacheOnlyCrossSystemFetchAgent([
        {
            "id": "REL-1",
            "ticket_id": "REL-1",
            "title": "Similar allocation NPE",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 8,
        },
        {
            "id": "REL-1",
            "ticket_id": "REL-1",
            "title": "Duplicate cache copy",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 7,
        },
        {
            "id": "REL-2",
            "ticket_id": "REL-2",
            "title": "Another related allocation issue",
            "source": "github",
            "source_id": "spark-github",
            "overlap_score": 6,
        },
    ])

    context = run(agent.run(primary_context()))

    ids = [item["ticket_id"] for item in context["related_tickets"]]
    assert ids == ["REL-1", "REL-2"]


def test_panel_2_output_never_includes_primary_ticket(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(cross_module, "ConnectorRegistry", EmptyRegistry)
    context = primary_context()
    agent = CacheOnlyCrossSystemFetchAgent([
        {
            "id": " sto-1089 ",
            "ticket_id": " sto-1089 ",
            "title": "Whitespace/lowercase primary duplicate",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 10,
        },
        {
            "id": "REL-3",
            "ticket_id": "REL-3",
            "title": "Different ticket",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 8,
        },
    ])

    context = run(agent.run(context))
    panel_2 = {"related_tickets": context["related_tickets"]}

    primary_id = context["primary_ticket"]["ticket_id"].strip().upper()
    panel_ids = {
        str(item.get("ticket_id") or item.get("id") or "").strip().upper()
        for item in panel_2["related_tickets"]
    }
    assert primary_id not in panel_ids
