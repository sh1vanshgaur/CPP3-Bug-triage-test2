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


# ── Tests for _get_family() ──────────────────────────────────────

def test_get_family_strips_jira_suffix():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("my-project-jira") == "my-project"


def test_get_family_strips_github_suffix():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("my-project-github") == "my-project"


def test_get_family_strips_bugzilla_suffix():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("spark-bugzilla") == "spark"


def test_get_family_strips_underscore_suffix():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("storage_team_github") == "storage_team"


def test_get_family_strips_jira_cloud_suffix():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("acme-jira-cloud") == "acme"


def test_get_family_groups_siblings():
    """Two connectors from the same project should have the same family."""
    agent = CrossSystemFetchAgent()
    family_jira = agent._get_family("spark-jira")
    family_gh = agent._get_family("spark-github")
    assert family_jira == family_gh == "spark"


def test_get_family_no_suffix_returns_full_id():
    agent = CrossSystemFetchAgent()
    assert agent._get_family("custom-system") == "custom-system"


# ── Tests for _select_targets() ──────────────────────────────────


class FakeConnector:
    """Minimal stand-in for BaseConnector used in targeting tests."""
    def __init__(self, source_id, system_type, is_bug_source=True):
        self.source_id = source_id
        self.system_type = system_type
        self.is_bug_source = is_bug_source


def test_select_targets_includes_all_external_bug_sources():
    agent = CrossSystemFetchAgent()
    connectors = [
        FakeConnector("proj-jira", "jira"),
        FakeConnector("proj-github", "github"),
        FakeConnector("proj-bugzilla", "bugzilla"),
        FakeConnector("other-jira", "jira"),
    ]
    targets = agent._select_targets(connectors, "proj-jira")
    target_ids = [c.source_id for c in targets]
    assert "proj-jira" not in target_ids  # excludes primary
    assert "proj-github" in target_ids
    assert "proj-bugzilla" in target_ids
    assert "other-jira" in target_ids
    assert len(targets) == 3


def test_select_targets_sisters_sorted_first():
    """Connectors from the same family should be sorted before others."""
    agent = CrossSystemFetchAgent()
    connectors = [
        FakeConnector("spark-jira", "jira"),         # primary
        FakeConnector("spark-github", "github"),      # sister
        FakeConnector("kafka-jira", "jira"),           # other
        FakeConnector("hadoop-bugzilla", "bugzilla"),  # other
    ]
    targets = agent._select_targets(connectors, "spark-jira")
    target_ids = [c.source_id for c in targets]
    # spark-github is a sister → should come first
    assert target_ids[0] == "spark-github"
    assert len(targets) == 3


def test_select_targets_excludes_non_bug_sources():
    agent = CrossSystemFetchAgent()
    connectors = [
        FakeConnector("proj-jira", "jira"),
        FakeConnector("proj-confluence", "confluence", is_bug_source=False),
        FakeConnector("proj-github", "github"),
    ]
    targets = agent._select_targets(connectors, "proj-jira")
    target_ids = [c.source_id for c in targets]
    assert "proj-confluence" not in target_ids
    assert "proj-github" in target_ids


def test_select_targets_returns_empty_when_no_external():
    agent = CrossSystemFetchAgent()
    connectors = [
        FakeConnector("proj-jira", "jira"),
    ]
    targets = agent._select_targets(connectors, "proj-jira")
    assert targets == []


def test_select_targets_caps_at_twelve():
    agent = CrossSystemFetchAgent()
    connectors = [FakeConnector("primary-jira", "jira")]
    for i in range(15):
        connectors.append(
            FakeConnector(f"ext-{i}-github", "github"))
    targets = agent._select_targets(connectors, "primary-jira")
    assert len(targets) == 12


# ── Test: Level 3 always runs (not just as fallback) ─────────────


class TrackingLiveCrossSystemFetchAgent(CrossSystemFetchAgent):
    """Subclass that tracks whether _live_api_search was called."""
    def __init__(self, cache_candidates):
        self.cache_candidates = cache_candidates
        self.live_search_called = False

    async def _scan_redis_cache(self, keywords):
        return list(self.cache_candidates)

    async def _live_api_search(
            self, primary, primary_source, context,
            all_connectors=None):
        self.live_search_called = True
        return [], ["external-github"]


def test_live_search_always_runs_even_with_cache_hits(monkeypatch):
    """Level 3 should always run even when Level 2 returns many hits."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(cross_module, "ConnectorRegistry", EmptyRegistry)
    # Provide 5 cache candidates — previously this would suppress Level 3
    many_cache_candidates = [
        {
            "id": f"CACHE-{i}",
            "ticket_id": f"CACHE-{i}",
            "title": f"Cache hit {i} for storage issue",
            "source": "jira-storage",
            "source_id": "jira-storage",
            "overlap_score": 8 - i,
        }
        for i in range(5)
    ]
    agent = TrackingLiveCrossSystemFetchAgent(many_cache_candidates)

    context = run(agent.run(primary_context()))

    assert agent.live_search_called, \
        "Level 3 live search should always run for external systems"
    assert "external-github" in context["sources_queried"]


# ── Tests for _extract_description_keywords() ────────────────────

def test_extract_description_keywords_finds_exceptions():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords(
        "Got a NullPointerException in StorageController.allocate "
        "when trying to provision VMs",
        "VM provisioning fails")
    assert "NullPointerException" in result


def test_extract_description_keywords_finds_camelcase():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords(
        "The UnsafeRowWriter class throws an error during "
        "SQL query execution",
        "SQL query fails")
    assert "UnsafeRowWriter" in result


def test_extract_description_keywords_finds_files():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords(
        "Build fails when processing pom.xml in the "
        "storage module",
        "Build failure")
    assert "pom.xml" in result


def test_extract_description_keywords_limits_to_four():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords(
        "NullPointerException ClassCastException "
        "StorageController UnsafeRowWriter DataFrameReader "
        "SparkSession pom.xml build.gradle config.yml "
        "something something",
        "Multiple errors")
    words = result.split()
    assert len(words) <= 4


def test_extract_description_keywords_handles_empty():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords("", "")
    assert result == ""


def test_extract_description_keywords_uses_title_too():
    agent = CrossSystemFetchAgent()
    result = agent._extract_description_keywords(
        "some short text",
        "StorageController NPE during provisioning")
    assert "StorageController" in result

