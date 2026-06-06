import asyncio
import json

from orchestrator.orchestrator import TaskOrchestrator


def run(coro):
    return asyncio.run(coro)


def primary_ticket():
    return {
        "ticket_id": "STO-1089",
        "title": "StorageController NPE during VM provisioning",
        "source_id": "jira-storage",
        "system_type": "jira",
    }


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.calls = []

    async def setex(self, key, ttl, value):
        self.calls.append(("setex", key, value))
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def rpush(self, key, value):
        self.calls.append(("rpush", key, value))
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start:end + 1]

    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))

    async def publish(self, channel, message):
        self.calls.append(("publish", channel, message))


def test_ai_prereq_accepts_related_issues_only():
    orch = TaskOrchestrator()
    context = {
        "primary_ticket": primary_ticket(),
        "related_issues": {
            "related_tickets": [{"ticket_id": "REL-1"}],
            "sources_queried": ["github"],
        },
        "knowledge_base": {},
    }

    assert orch._missing_ai_requirements(context) == []
    assert context["related_tickets"] == [{"ticket_id": "REL-1"}]
    assert context["kb_articles"] == []


def test_ai_prereq_accepts_empty_related_and_kb_lists():
    orch = TaskOrchestrator()
    context = {
        "primary_ticket": primary_ticket(),
        "related_tickets": [],
        "kb_articles": [],
    }

    assert orch._missing_ai_requirements(context) == []
    assert context["related_tickets"] == []
    assert context["kb_articles"] == []


def test_ai_summary_skip_publishes_failed_panel(monkeypatch):
    fake = FakeRedis()

    async def get_fake_redis():
        return fake

    monkeypatch.setattr("orchestrator.redis_client.get_redis", get_fake_redis)

    context = {"errors": {"pipeline": "AISynthesisAgent skipped"}}
    run(TaskOrchestrator()._publish_ai_summary_failed("case-1", context))

    message = json.loads(fake.values["panel:case-1:ai_summary"])
    assert message["panel"] == "ai_summary"
    assert message["status"] == "failed"
    assert message["data"]["errors"]["pipeline"] == "AISynthesisAgent skipped"


def test_pipeline_done_publishes_after_ai_summary_failed(monkeypatch):
    fake = FakeRedis()

    async def get_fake_redis():
        return fake

    monkeypatch.setattr("orchestrator.redis_client.get_redis", get_fake_redis)

    orch = TaskOrchestrator()
    run(orch._publish_ai_summary_failed("case-1", {"errors": {}}))
    run(orch._publish_complete("case-1", {}, 123))

    published = [
        json.loads(call[2])
        for call in fake.calls
        if call[0] == "publish"
    ]
    assert published[0]["panel"] == "ai_summary"
    assert published[0]["status"] == "failed"
    assert published[-1]["type"] == "pipeline_done"
