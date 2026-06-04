import asyncio
import os
from typing import Protocol

import structlog

from .base_connector import BaseConnector
from .bugzilla_connector import BugzillaConnector
from .confluence_connector import ConfluenceConnector
from .customer_portal_connector import CustomerPortalConnector
from .github_connector import GithubConnector
from .jira_connector import JiraConnector
from .support_kb_connector import SupportKBConnector

log = structlog.get_logger()


class TokenProvider(Protocol):
    def get_token(self, secret_ref: str | None) -> str:
        ...


class EnvironmentTokenProvider:
    """Current token mechanism; replace internally when Vault is wired."""

    def get_token(self, secret_ref: str | None) -> str:
        if not secret_ref:
            return ""
        return os.environ.get(secret_ref, "")


SYSTEM_TYPE_TO_CLASS = {
    "github":          GithubConnector,
    "jira":            JiraConnector,
    "jira_apache":     JiraConnector,
    "jira_cloud":      JiraConnector,
    "bugzilla":        BugzillaConnector,
    "confluence":      ConfluenceConnector,
    "support_kb":      SupportKBConnector,
    "customer_portal": CustomerPortalConnector,
}

_connector_cache: list[BaseConnector] = []
_token_provider: TokenProvider = EnvironmentTokenProvider()


def get_connector_class(system_type: str):
    return SYSTEM_TYPE_TO_CLASS.get((system_type or "").strip().lower())


def set_token_provider(provider: TokenProvider) -> None:
    global _token_provider
    _token_provider = provider


async def load_connectors_from_db() -> list[BaseConnector]:
    global _connector_cache

    try:
        from ..db.session import AsyncSessionLocal
        from ..db.repositories.source_registry import get_enabled_sources

        async with AsyncSessionLocal() as db:
            sources = await get_enabled_sources(db)
    except Exception as e:
        log.warning("ConnectorRegistry: failed to read source_registry",
                    error=str(e))
        return []

    connectors: list[BaseConnector] = []
    for row in sources:
        system_type = (row.system_type or "").strip().lower()
        connector_class = get_connector_class(system_type)
        if not connector_class:
            log.warning("ConnectorRegistry: unknown system_type",
                        source_id=row.source_id,
                        system_type=row.system_type)
            continue

        try:
            token = _token_provider.get_token(row.auth_secret_ref)
        except Exception as e:
            log.warning("ConnectorRegistry: token load failed",
                        source_id=row.source_id,
                        secret_ref=row.auth_secret_ref,
                        error=str(e))
            token = ""

        try:
            connector = connector_class(
                source_id=row.source_id,
                system_type=system_type,
                base_url=row.base_url or "",
                project_key=row.project_key or "",
                ticket_prefix=row.ticket_prefix or "",
                token=token,
            )
            connector.display_name = row.display_name
            connector.auth_type = row.auth_type
            connector.auth_secret_ref = row.auth_secret_ref
            connector.port = row.port
            connectors.append(connector)
        except Exception as e:
            log.warning("ConnectorRegistry: connector init failed",
                        source_id=row.source_id,
                        system_type=system_type,
                        error=str(e))

    _connector_cache = connectors
    return connectors


class ConnectorRegistry:
    @classmethod
    async def get_all_enabled(cls) -> list[BaseConnector]:
        return await load_connectors_from_db()

    @classmethod
    async def get_all_connectors(cls) -> list[BaseConnector]:
        return await cls.get_all_enabled()

    @classmethod
    async def get(cls, source_id: str) -> BaseConnector | None:
        if not source_id:
            return None
        connectors = await cls.get_all_enabled()
        for connector in connectors:
            if connector.source_id == source_id:
                return connector
        return None

    @classmethod
    async def get_connector(cls, source_id: str) -> BaseConnector | None:
        return await cls.get(source_id)

    @classmethod
    async def get_by_ticket_id(cls, ticket_id: str) -> BaseConnector | None:
        if not ticket_id:
            return None

        ticket_upper = ticket_id.upper().strip()
        connectors = [
            connector
            for connector in await cls.get_all_enabled()
            if connector.is_bug_source
        ]
        connectors.sort(
            key=lambda connector: len(connector.ticket_prefix or ""),
            reverse=True,
        )

        for connector in connectors:
            prefix = (connector.ticket_prefix or "").upper().strip()
            if not prefix:
                continue
            if (ticket_upper == prefix
                    or ticket_upper.startswith(f"{prefix}-")
                    or ticket_upper.startswith(prefix)):
                return connector

        if ticket_id.isdigit():
            github_connectors = [
                connector for connector in connectors
                if connector.system_type == "github"
            ]
            if len(github_connectors) == 1:
                return github_connectors[0]

        return None

    @classmethod
    async def get_by_type(cls, system_type: str) -> BaseConnector | None:
        connectors = await cls.get_all_by_type(system_type)
        if len(connectors) > 1:
            log.warning("ConnectorRegistry: get_by_type returned first of many",
                        system_type=system_type,
                        count=len(connectors))
        return connectors[0] if connectors else None

    @classmethod
    async def get_all_by_type(cls, system_type: str) -> list[BaseConnector]:
        normalized = (system_type or "").strip().lower()
        connectors = await cls.get_all_enabled()
        return [
            connector for connector in connectors
            if connector.system_type == normalized
        ]

    @classmethod
    async def health_check_all(cls, timeout: float = 10.0) -> list[dict]:
        connectors = await cls.get_all_enabled()

        async def check_one(connector: BaseConnector) -> dict:
            try:
                result = await asyncio.wait_for(
                    connector.health_check(),
                    timeout=timeout,
                )
                result.setdefault("source_id", connector.source_id)
                result.setdefault("system_type", connector.system_type)
                result.setdefault(
                    "display_name",
                    getattr(connector, "display_name", connector.source_id),
                )
                return result
            except asyncio.TimeoutError:
                return {
                    "source_id": connector.source_id,
                    "system_type": connector.system_type,
                    "display_name": getattr(
                        connector, "display_name", connector.source_id),
                    "status": "timeout",
                    "ok": False,
                    "latency_ms": int(timeout * 1000),
                    "error": "health check timed out",
                }
            except Exception as e:
                return {
                    "source_id": connector.source_id,
                    "system_type": connector.system_type,
                    "display_name": getattr(
                        connector, "display_name", connector.source_id),
                    "status": "error",
                    "ok": False,
                    "latency_ms": 0,
                    "error": str(e)[:300],
                }

        return await asyncio.gather(
            *[check_one(connector) for connector in connectors]
        )

    @classmethod
    def invalidate_cache(cls) -> None:
        global _connector_cache
        _connector_cache = []


def get_connector_for_ticket(ticket_id: str) -> BaseConnector | None:
    ticket_upper = (ticket_id or "").upper().strip()
    if not ticket_upper:
        return None

    connectors = [
        connector for connector in _connector_cache
        if connector.is_bug_source
    ]
    connectors.sort(
        key=lambda connector: len(connector.ticket_prefix or ""),
        reverse=True,
    )

    for connector in connectors:
        prefix = (connector.ticket_prefix or "").upper().strip()
        if not prefix:
            continue
        if (ticket_upper == prefix
                or ticket_upper.startswith(f"{prefix}-")
                or ticket_upper.startswith(prefix)):
            return connector
    return None
