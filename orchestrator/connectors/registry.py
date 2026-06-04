import asyncio
import os
import structlog
from .github_connector import GithubConnector
from .jira_connector import JiraConnector
from .bugzilla_connector import BugzillaConnector
from .confluence_connector import ConfluenceConnector
from .customer_portal_connector import CustomerPortalConnector
from .base_connector import BaseConnector

log = structlog.get_logger()

SYSTEM_TYPE_TO_CLASS = {
    "github":          GithubConnector,
    "jira":            JiraConnector,
    "jira_apache":     JiraConnector,
    "jira_cloud":      JiraConnector,
    "bugzilla":        BugzillaConnector,
    "confluence":      ConfluenceConnector,
    "customer_portal": CustomerPortalConnector,
}

_connector_cache: list[BaseConnector] = []
_cache_loaded: bool = False


async def load_connectors_from_db() -> list[BaseConnector]:
    global _connector_cache, _cache_loaded
    try:
        from ..db.session import AsyncSessionLocal
        from ..db.repositories.source_registry import get_enabled_sources
        async with AsyncSessionLocal() as db:
            sources = await get_enabled_sources(db)
        connectors = []
        for row in sources:
            cls = SYSTEM_TYPE_TO_CLASS.get(row.system_type)
            if not cls:
                log.warning("Unknown system_type", system_type=row.system_type)
                continue
            token = os.environ.get(row.auth_secret_ref or "", "")
            try:
                connector = cls(
                    source_id=row.source_id,
                    system_type=row.system_type,
                    base_url=row.base_url,
                    project_key=row.project_key or "",
                    ticket_prefix=row.ticket_prefix or "",
                    token=token,
                )
                connectors.append(connector)
            except Exception as e:
                log.warning("Failed to init connector", source_id=row.source_id, error=str(e))
        _connector_cache = connectors
        _cache_loaded = True
        return connectors
    except Exception as e:
        log.warning("Failed to load connectors from DB", error=str(e))
        return _connector_cache


class ConnectorRegistry:
    @classmethod
    async def get_all_enabled(cls) -> list[BaseConnector]:
        global _connector_cache, _cache_loaded

        # Reload if not loaded OR if previous load returned empty (e.g. DB wasn't ready)
        if not _cache_loaded or len(_connector_cache) == 0:
            loaded = await load_connectors_from_db()
            if loaded:
                _connector_cache = loaded
                _cache_loaded = True
            elif not _cache_loaded:
                # First attempt failed — retry once after a brief pause
                await asyncio.sleep(0.5)
                loaded = await load_connectors_from_db()
                _connector_cache = loaded
                _cache_loaded = bool(loaded)

        return _connector_cache

    @classmethod
    async def get_all_connectors(cls) -> list[BaseConnector]:
        return await cls.get_all_enabled()


    @classmethod
    async def get_connector(cls, source_id: str) -> BaseConnector | None:
        connectors = await cls.get_all_enabled()
        for c in connectors:
            if c.source_id == source_id:
                return c
        return None

    @classmethod
    async def get_by_type(cls, system_type: str) -> BaseConnector | None:
        connectors = await cls.get_all_enabled()
        for c in connectors:
            if c.system_type == system_type:
                return c
        return None

    @classmethod
    async def get_all_by_type(cls,
                               system_type: str) -> list[BaseConnector]:
        connectors = await cls.get_all_enabled()
        return [
            c for c in connectors
            if c.system_type == system_type
        ]

    @classmethod
    def invalidate_cache(cls) -> None:
        global _connector_cache, _cache_loaded
        _connector_cache = []
        _cache_loaded = False


def get_connector_for_ticket(ticket_id: str) -> BaseConnector | None:
    for c in _connector_cache:
        prefix = c.ticket_prefix.upper()
        if ticket_id.upper().startswith(prefix):
            return c
    return None
