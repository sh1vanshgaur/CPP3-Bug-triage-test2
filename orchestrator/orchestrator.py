import asyncio
import json
import time
import structlog
from .agents import ContextFetchAgent, CrossSystemFetchAgent, EnrichmentAgent, AISynthesisAgent
from .db.session import AsyncSessionLocal
from .db.repositories.pipeline_context import (
    create_pipeline_context, get_pipeline_context,
    update_pipeline_step, delete_pipeline_context, get_steps_to_run,
)
from .db.repositories.audit_log import insert_audit_entry
from .redis_client import (
    get_redis,
    cache_case_result,
    store_panel_update,
    publish_pipeline_done,
)

log = structlog.get_logger()


class TaskOrchestrator:
    async def run(self, case_id: str, bug_id: str, source_id: str, engineer_id: str, force_refresh: bool = False) -> None:
        # Give the frontend 1.5 s to open WebSocket and subscribe before we start
        # publishing panels. This prevents the race condition where Panel 1 is
        # published before anyone is listening.
        await asyncio.sleep(1.5)

        start_time = time.monotonic()
        context = {
            "case_id": case_id,
            "bug_id": bug_id,
            "source_id": source_id,
            "engineer_id": engineer_id,
            "force_refresh": force_refresh,
            "errors": {},
        }
        log.info("Orchestrator context initialized",
                 case_id=case_id,
                 ticket_id=bug_id,
                 source_id=source_id)

        async with AsyncSessionLocal() as db:
            existing = await get_pipeline_context(db, case_id)
            if existing:
                resume_step = existing.current_step
                if existing.context_json:
                    context.update(existing.context_json)
                log.info("Resuming pipeline", case_id=case_id, from_step=resume_step)
            else:
                await create_pipeline_context(db, case_id, {})
                resume_step = "start"

        steps_to_run = get_steps_to_run(resume_step)

        if "context_fetch" in steps_to_run:
            log.info("Pipeline agent start",
                     case_id=case_id,
                     agent="ContextFetchAgent",
                     context_keys=self._context_keys(context))
            context, duration_ms = await ContextFetchAgent().safe_run(context)
            log.info("ContextFetchAgent completed",
                     case_id=case_id,
                     ticket_id=bug_id,
                     source_id=context.get("source_id", source_id),
                     has_primary_ticket=bool(context.get("primary_ticket")),
                     title=(
                         (context.get("primary_ticket") or {})
                         .get("title", "")[:80]
                     ),
                     duration_ms=duration_ms,
                     context_keys=self._context_keys(context))
            await self._checkpoint(case_id, "context_fetch", context)
            await self._publish_panel(case_id, "bug_context", {
                "primary_ticket": context.get("primary_ticket"),
                "bug_context": context.get("bug_context") or {},
                "components": context.get("components") or [],
                "customer_cases": context.get("customer_cases") or [],
                "source_references": context.get("source_references") or [],
                "errors": context.get("errors") or {},
            }, agent="ContextFetchAgent", status="completed")

        if "cross_system_fetch" in steps_to_run or "enrichment" in steps_to_run:
            if not self._has_primary_ticket(context):
                self._add_pipeline_error(
                    context,
                    "ContextFetchAgent did not produce primary_ticket; "
                    "skipping Phase 2 and AI synthesis.",
                )
                log.warning("Pipeline phase skipped",
                            case_id=case_id,
                            phase="phase_2",
                            reason="missing_primary_ticket",
                            context_keys=self._context_keys(context))
                steps_to_run = [
                    step for step in steps_to_run
                    if step not in {
                        "cross_system_fetch",
                        "enrichment",
                        "ai_synthesis",
                    }
                ]
            else:
                log.info("Pipeline phase start",
                         case_id=case_id,
                         phase="phase_2",
                         context_keys=self._context_keys(context))

        if "cross_system_fetch" in steps_to_run or "enrichment" in steps_to_run:
            run_cross = "cross_system_fetch" in steps_to_run
            run_enrich = "enrichment" in steps_to_run

            if run_cross and run_enrich:
                log.info("Pipeline agent start",
                         case_id=case_id,
                         agent="CrossSystemFetchAgent",
                         context_keys=self._context_keys(context))
                log.info("Pipeline agent start",
                         case_id=case_id,
                         agent="EnrichmentAgent",
                         context_keys=self._context_keys(context))
                results = await asyncio.gather(
                    CrossSystemFetchAgent().safe_run(context),
                    EnrichmentAgent().safe_run(context),
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, Exception):
                        log.warning("Phase 2 agent raised exception", error=str(res))
                    else:
                        ctx_result, _ = res
                        context.update(ctx_result)
                log.info("Pipeline agent finish",
                         case_id=case_id,
                         agent="CrossSystemFetchAgent",
                         related_count=len(context.get("related_tickets") or []),
                         context_keys=self._context_keys(context))
                log.info("Pipeline agent finish",
                         case_id=case_id,
                         agent="EnrichmentAgent",
                         kb_count=len(context.get("kb_articles") or []),
                         context_keys=self._context_keys(context))
            elif run_cross:
                log.info("Pipeline agent start",
                         case_id=case_id,
                         agent="CrossSystemFetchAgent",
                         context_keys=self._context_keys(context))
                context, duration_ms = await CrossSystemFetchAgent().safe_run(context)
                log.info("Pipeline agent finish",
                         case_id=case_id,
                         agent="CrossSystemFetchAgent",
                         duration_ms=duration_ms,
                         related_count=len(context.get("related_tickets") or []),
                         context_keys=self._context_keys(context))
            elif run_enrich:
                log.info("Pipeline agent start",
                         case_id=case_id,
                         agent="EnrichmentAgent",
                         context_keys=self._context_keys(context))
                context, duration_ms = await EnrichmentAgent().safe_run(context)
                log.info("Pipeline agent finish",
                         case_id=case_id,
                         agent="EnrichmentAgent",
                         duration_ms=duration_ms,
                         kb_count=len(context.get("kb_articles") or []),
                         context_keys=self._context_keys(context))

            context["related_issues"] = {
                "related_tickets": self._related_tickets_from_context(context),
                "sources_queried": context.get("sources_queried") or [],
            }
            context["related_tickets"] = self._related_tickets_from_context(context)
            context["knowledge_base"] = {
                "kb_articles": context.get("kb_articles") or [],
                "kb_reasoning": context.get("kb_reasoning") or "",
            }
            context["kb_articles"] = context.get("kb_articles") or []

            await self._checkpoint(case_id, "enrichment", context)
            await self._publish_panel(case_id, "related_issues", {
                "related_tickets": self._related_tickets_from_context(context),
                "sources_queried": context.get("sources_queried") or [],
            }, agent="CrossSystemFetchAgent", status="completed")
            await self._publish_panel(case_id, "linked_context", {
                "kb_articles": context.get("kb_articles") or [],
                "kb_reasoning": context.get("kb_reasoning") or "",
                "customer_cases": context.get("customer_cases") or [],
            }, agent="EnrichmentAgent", status="completed")
            # BUG3: persist Panel 2/3 payloads separately so /cases/{id}
            # can recover them even after the main case cache expires
            try:
                import json as _json
                _r = await get_redis()
                await _r.setex(
                    f"related:{case_id}", 3600,
                    _json.dumps(context.get("related_tickets") or []))
                await _r.setex(
                    f"enrichment:{case_id}", 3600,
                    _json.dumps(context.get("enrichment_sources") or []))
                await _r.setex(
                    f"kb:{case_id}", 3600,
                    _json.dumps(context.get("kb_articles") or []))
            except Exception:
                pass

            group_id = await self._persist_related_group(context)
            if group_id:
                context["group_id"] = group_id

        if "ai_synthesis" in steps_to_run:
            self._normalize_ai_inputs(context)
            missing = self._missing_ai_requirements(context)
            if missing:
                message = (
                    "AISynthesisAgent skipped; missing upstream context: "
                    + ", ".join(missing)
                )
                self._add_pipeline_error(context, message)
                log.warning("Pipeline agent skipped",
                            case_id=case_id,
                            agent="AISynthesisAgent",
                            missing=missing,
                            context_keys=self._context_keys(context))
                await self._publish_ai_summary_failed(case_id, context)
            else:
                log.info("Pipeline agent start",
                         case_id=case_id,
                         agent="AISynthesisAgent",
                         context_keys=self._context_keys(context))
                context, duration_ms = await AISynthesisAgent().safe_run(context)
                log.info("Pipeline agent finish",
                         case_id=case_id,
                         agent="AISynthesisAgent",
                         duration_ms=duration_ms,
                         has_synthesis=bool(context.get("synthesis")),
                         context_keys=self._context_keys(context))
                await self._checkpoint(case_id, "ai_synthesis", context)
                await self._publish_panel(case_id, "ai_summary", {
                    "synthesis": context.get("synthesis") or {},
                    "errors": context.get("errors") or {},
                }, agent="AISynthesisAgent", status="completed")

        total_ms = int((time.monotonic() - start_time) * 1000)
        synthesis = context.get("synthesis") or {}

        # Publish pipeline_complete IMMEDIATELY — before DB writes
        # so WebSocket receives it before it can disconnect
        await self._publish_complete(case_id, synthesis, total_ms)
        log.info("Pipeline complete", case_id=case_id, duration_ms=total_ms)

        # DB writes happen after WebSocket is notified
        await cache_case_result(case_id, {
            "case_id": case_id,
            "bug_id": bug_id,
            "source_id": source_id,
            "context": context,
        }, ttl=86400)

        async with AsyncSessionLocal() as db:
            await insert_audit_entry(db, {
                "case_id": case_id,
                "bug_id": bug_id,
                "source_id": source_id,
                "engineer_id": engineer_id,
                "step": "pipeline_complete",
                "status": "done",
                "summary": {
                    "severity": synthesis.get("unified_severity"),
                    "ai_severity": synthesis.get("unified_severity"),
                    "confidence": synthesis.get("confidence"),
                    "root_cause": synthesis.get("root_cause", "")[:500],
                    "recommended_actions": synthesis.get("recommended_actions", [])[:3],
                    "engineer_summary": synthesis.get("engineer_summary", "")[:500],
                    "status_summary": synthesis.get("status_summary", ""),
                    "ticket_updated_at": (context.get("primary_ticket") or {}).get("updated_at", ""),
                    "ticket_severity": (context.get("primary_ticket") or {}).get("severity", ""),
                    "ticket_status": (context.get("primary_ticket") or {}).get("status", ""),
                    "updated_at": (context.get("primary_ticket") or {}).get("updated_at", ""),
                    "status": (context.get("primary_ticket") or {}).get("status", ""),
                    "used_fallback": synthesis.get("used_fallback", False),
                    "group_id": context.get("group_id"),
                    "related_tickets": self._audit_related_tickets(
                        context.get("related_tickets") or []),
                },
                "systems_queried": context.get("sources_queried", []),
                "duration_ms": total_ms,
            })
            await delete_pipeline_context(db, case_id)

        # Invalidate bug list cache after triage completes so
        # the next GET /bugs reflects the new triage_info.
        try:
            _r = await get_redis()
            _keys = await _r.keys("bug_list:*")
            if _keys:
                await _r.delete(*_keys)
        except Exception:
            pass  # cache invalidation must never crash the pipeline

    async def _checkpoint(self, case_id: str, step: str, context: dict) -> None:
        try:
            async with AsyncSessionLocal() as db:
                safe_ctx = {k: v for k, v in context.items() if k != "errors"}
                await update_pipeline_step(db, case_id, step, safe_ctx)
        except Exception as e:
            log.warning("Checkpoint failed", step=step, error=str(e))

    async def _publish_panel(self, case_id: str,
                              panel_name: str,
                              data: dict,
                              agent: str = "",
                              status: str = "completed") -> None:
        try:
            from .redis_client import get_redis as _get_redis
            r = await _get_redis()
            message = await store_panel_update(
                case_id=case_id,
                panel_name=panel_name,
                data=data,
                agent=agent,
                status=status,
            )

            # Publish to live listeners
            await r.publish(f"ws:{case_id}", message)
            log.info("Panel published",
                     case_id=case_id,
                     panel=panel_name,
                     agent=agent,
                     status=status,
                     data_keys=sorted(data.keys()))
        except Exception as e:
            log.warning("Panel publish failed",
                        panel=panel_name, error=str(e))

    async def _publish_complete(self, case_id: str,
                                 synthesis: dict,
                                 duration_ms: int) -> None:
        try:
            await publish_pipeline_done(
                case_id=case_id,
                status="completed",
                duration_ms=duration_ms,
                severity=synthesis.get("unified_severity"),
                confidence=synthesis.get("confidence"),
            )

        except Exception as e:
            log.warning("publish_complete failed", error=str(e))

    async def _publish_ai_summary_failed(self, case_id: str, context: dict) -> None:
        await self._publish_panel(case_id, "ai_summary", {
            "synthesis": context.get("synthesis") or {},
            "errors": context.get("errors") or {},
        }, agent="AISynthesisAgent", status="failed")

    def _has_primary_ticket(self, context: dict) -> bool:
        primary = context.get("primary_ticket")
        return bool(
            isinstance(primary, dict)
            and primary.get("ticket_id")
            and primary.get("title")
        )

    def _missing_ai_requirements(self, context: dict) -> list[str]:
        self._normalize_ai_inputs(context)
        missing = []
        if not self._has_primary_ticket(context):
            missing.append("primary_ticket")
        if ("related_tickets" not in context
                and "related_issues" not in context):
            missing.append("related_tickets")
        if ("kb_articles" not in context
                and "knowledge_base" not in context):
            missing.append("kb_articles")
        return missing

    def _normalize_ai_inputs(self, context: dict) -> None:
        if "related_tickets" not in context and "related_issues" in context:
            context["related_tickets"] = self._related_tickets_from_context(context)
        elif "related_tickets" in context:
            context["related_tickets"] = context.get("related_tickets") or []

        if "related_issues" not in context:
            context["related_issues"] = {
                "related_tickets": context.get("related_tickets") or [],
                "sources_queried": context.get("sources_queried") or [],
            }

        if "kb_articles" not in context and "knowledge_base" in context:
            kb = context.get("knowledge_base") or {}
            if isinstance(kb, dict):
                context["kb_articles"] = kb.get("kb_articles") or []
            else:
                context["kb_articles"] = []
        elif "kb_articles" in context:
            context["kb_articles"] = context.get("kb_articles") or []

        if "knowledge_base" not in context:
            context["knowledge_base"] = {
                "kb_articles": context.get("kb_articles") or [],
                "kb_reasoning": context.get("kb_reasoning") or "",
            }

    def _related_tickets_from_context(self, context: dict) -> list:
        related = context.get("related_tickets")
        if isinstance(related, list):
            return related
        related_issues = context.get("related_issues")
        if isinstance(related_issues, dict):
            nested = related_issues.get("related_tickets")
            return nested if isinstance(nested, list) else []
        if isinstance(related_issues, list):
            return related_issues
        return []

    def _add_pipeline_error(self, context: dict, message: str) -> None:
        context.setdefault("errors", {})
        context["errors"]["pipeline"] = message

    def _context_keys(self, context: dict) -> list[str]:
        return sorted(k for k in context.keys() if k != "errors")

    async def _persist_related_group(self, context: dict) -> str | None:
        primary = context.get("primary_ticket") or {}
        related = context.get("related_tickets") or []
        if not primary or not related:
            return None
        try:
            from .db.repositories.group_registry import (
                persist_related_issue_group)
            async with AsyncSessionLocal() as db:
                group_id = await persist_related_issue_group(
                    db, primary, related)
            if group_id:
                log.info("Related issue group persisted",
                         case_id=context.get("case_id"),
                         group_id=group_id,
                         related_count=len(related))
            return group_id
        except Exception as e:
            log.warning("Related group persistence failed",
                        case_id=context.get("case_id"),
                        error=str(e))
            return None

    def _audit_related_tickets(self, related: list[dict]) -> list[dict]:
        output = []
        for item in related[:20]:
            output.append({
                "ticket_id": (
                    item.get("ticket_id")
                    or item.get("id")
                    or item.get("key")
                    or ""),
                "source": (
                    item.get("source")
                    or item.get("system_type")
                    or item.get("source_id")
                    or ""),
                "title": item.get("title", "")[:200],
                "url": item.get("url", ""),
                "status": item.get("status", ""),
                "severity": item.get("severity", ""),
                "similarity_score": (
                    item.get("similarity_score")
                    or item.get("relevance_score")),
                "similarity_label": item.get("similarity_label", ""),
                "similarity_reason": (
                    item.get("similarity_reason", "")[:300]),
            })
        return output
