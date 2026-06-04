import asyncio
import dataclasses
import re
import structlog

from .base import BaseAgent
from ..connectors.registry import ConnectorRegistry

log = structlog.get_logger()

MAX_DESC = 8000
MAX_ERR  = 3000


class ContextFetchAgent(BaseAgent):
    step_name = "context_fetch"

    async def run(self, context: dict) -> dict:
        bug_id    = context.get("bug_id", "")
        source_id = context.get("source_id", "")

        log.info("ContextFetch start",
                 bug_id=bug_id, source_id=source_id)

        connector = await self._resolve_connector(source_id, bug_id)

        if connector is None:
            log.error("ContextFetch: no connector found",
                      bug_id=bug_id, source_id=source_id)
            self._add_error(context,
                f"No connector resolved for bug_id={bug_id}")
            context["primary_ticket"]  = None
            context["linked_items"]    = []
            context["customer_cases"]  = []
            context["components"]      = []
            return context

        log.info("ContextFetch: connector resolved",
                 connector=connector.source_id,
                 ctype=type(connector).__name__)

        # ── Fetch primary ticket ──────────────────────────────────
        ticket = None
        try:
            ticket = await asyncio.wait_for(
                connector.get(bug_id), timeout=15.0)
        except asyncio.TimeoutError:
            log.error("ContextFetch: GET timed out", bug_id=bug_id)
            self._add_error(context, f"Timeout fetching {bug_id}")
        except Exception as e:
            log.error("ContextFetch: GET error",
                      bug_id=bug_id, err=str(e))
            self._add_error(context, str(e))

        if ticket is None:
            log.error("ContextFetch: ticket is None",
                      bug_id=bug_id, connector=connector.source_id)
            context["primary_ticket"]  = None
            context["linked_items"]    = []
            context["customer_cases"]  = []
            context["components"]      = []
            return context

        log.info("ContextFetch: ticket OK",
                 id=ticket.ticket_id,
                 title=(ticket.title or "")[:60],
                 severity=ticket.severity,
                 component=ticket.component)

        # ── Truncate oversized fields ─────────────────────────────
        desc = ticket.description or ""
        if len(desc) > MAX_DESC:
            lines = desc.splitlines()
            desc = ("\n".join(lines[:100])
                    + "\n\n[...truncated...]\n\n"
                    + "\n".join(lines[-100:]))

        err = ticket.error_excerpt or ""
        if len(err) > MAX_ERR:
            lines = err.splitlines()
            err = ("\n".join(lines[:50])
                   + "\n\n[...truncated...]\n\n"
                   + "\n".join(lines[-50:]))

        # ── Co-reference extraction from ticket text ──────────────
        # Deterministically extract explicit cross-system references
        # from the ticket body before doing any LLM search
        raw_text = f"{ticket.title} {desc} {err}"
        co_refs = self._extract_co_references(raw_text)
        if co_refs:
            log.info("ContextFetch: co-refs found",
                     count=len(co_refs), refs=co_refs[:3])

        # ── Fetch linked items ────────────────────────────────────
        linked_items = []
        try:
            linked_items = await asyncio.wait_for(
                connector.get_linked_items(bug_id),
                timeout=8.0)
            log.info("ContextFetch: linked items",
                     count=len(linked_items))
        except Exception as e:
            log.warning("ContextFetch: linked_items failed",
                        err=str(e))

        # ── Fetch customer cases ──────────────────────────────────
        customer_cases = []
        try:
            portal = await ConnectorRegistry.get_by_type(
                "customer_portal")
            if portal:
                q = (f"{ticket.title} "
                     f"{ticket.component or ''}").strip()
                results = await asyncio.wait_for(
                    portal.search(q, max_results=3),
                    timeout=5.0)
                customer_cases = [
                    {
                        "case_id":  t.ticket_id,
                        "customer": t.reporter,
                        "title":    t.title,
                        "severity": t.severity,
                        "impact":   t.description,
                        "status":   t.status,
                    }
                    for t in results
                ]
                log.info("ContextFetch: customer cases",
                         count=len(customer_cases))
        except Exception as e:
            log.warning("ContextFetch: portal failed", err=str(e))

        # ── Build context dict ────────────────────────────────────
        ticket_dict = dataclasses.asdict(ticket)
        ticket_dict["description"]   = desc
        ticket_dict["error_excerpt"] = err

        context["primary_ticket"]  = ticket_dict
        context["linked_items"]    = linked_items
        context["co_references"]   = co_refs
        context["customer_cases"]  = customer_cases
        context["components"]      = (
            [ticket.component] if ticket.component else [])
        context["source_id"]       = connector.source_id
        context["direct_reference_links"] = getattr(ticket, "direct_reference_links", [])

        log.info("ContextFetch complete",
                 bug_id=bug_id,
                 has_ticket=True,
                 linked=len(linked_items),
                 cases=len(customer_cases),
                 co_refs=len(co_refs))
        return context

    # ── Connector resolution (longest-prefix-first) ───────────────
    async def _resolve_connector(self, source_id: str, bug_id: str):
        # 1. Direct source_id match
        if source_id:
            try:
                c = await ConnectorRegistry.get_connector(source_id)
                if c:
                    return c
            except Exception:
                pass

        # 2. Longest-prefix-first match
        try:
            all_connectors = await ConnectorRegistry.get_all_enabled()
        except Exception as e:
            log.error("ContextFetch: registry failed", err=str(e))
            return None

        excluded = {"confluence", "customer_portal"}
        candidates = [
            c for c in all_connectors
            if c.system_type not in excluded
        ]

        bug_upper = bug_id.upper()
        candidates.sort(
            key=lambda c: len(c.ticket_prefix or ""),
            reverse=True)

        for c in candidates:
            prefix = (c.ticket_prefix or "").upper().strip()
            if prefix and bug_upper.startswith(prefix):
                log.info("ContextFetch: prefix match",
                         prefix=prefix,
                         connector=c.source_id)
                return c

        # 3. Numeric ID → first GitHub connector
        if bug_id.isdigit():
            for c in candidates:
                if "github" in (c.system_type or "").lower():
                    return c

        return None

    # ── Deterministic co-reference extractor ─────────────────────
    def _extract_co_references(self, text: str) -> list:
        refs = []
        words = text.replace("#", " PR-").replace(":", " ").split()
        for word in words:
            w = word.strip(".,()[]")
            # JIRA format: PROJECT-12345
            if "-" in w:
                parts = w.split("-")
                if (len(parts) == 2
                        and parts[0].isupper()
                        and len(parts[0]) >= 2
                        and parts[1].isdigit()
                        and len(parts[1]) >= 3):
                    refs.append({
                        "raw_id": w,
                        "source": "JIRA",
                        "type":   "co_reference"
                    })
            # GitHub PR/issue
            if w.upper().startswith(("PR-", "GH-")):
                parts = w.split("-")
                if len(parts) == 2 and parts[1].isdigit():
                    refs.append({
                        "raw_id": parts[1],
                        "source": "GitHub",
                        "type":   "co_reference"
                    })
        # Remove duplicates
        seen = set()
        unique = []
        for r in refs:
            k = r["raw_id"]
            if k not in seen:
                seen.add(k)
                unique.append(r)
        return unique
