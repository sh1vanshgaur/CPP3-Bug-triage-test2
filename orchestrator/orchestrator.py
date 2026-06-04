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
from .redis_client import get_redis, cache_case_result

log = structlog.get_logger()


class TaskOrchestrator:
    async def run(self, case_id: str, bug_id: str, source_id: str, engineer_id: str) -> None:
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
            "errors": {},
        }

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
            context, _ = await ContextFetchAgent().safe_run(context)
            await self._checkpoint(case_id, "context_fetch", context)
            await self._publish_panel(case_id, "bug_context", {
                "primary_ticket": context.get("primary_ticket"),
                "keywords": context.get("keywords") or [],
                "components": context.get("components") or [],
                "customer_cases": context.get("customer_cases") or [],
                "errors": context.get("errors") or {},
            })

        if "cross_system_fetch" in steps_to_run or "enrichment" in steps_to_run:
            run_cross = "cross_system_fetch" in steps_to_run
            run_enrich = "enrichment" in steps_to_run

            if run_cross and run_enrich:
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
            elif run_cross:
                context, _ = await CrossSystemFetchAgent().safe_run(context)
            elif run_enrich:
                context, _ = await EnrichmentAgent().safe_run(context)

            await self._checkpoint(case_id, "enrichment", context)
            await self._publish_panel(case_id, "related_issues", {
                "related_tickets": context.get("related_tickets") or [],
                "sources_queried": context.get("sources_queried") or [],
            })
            await self._publish_panel(case_id, "linked_context", {
                "kb_articles": context.get("kb_articles") or [],
                "kb_reasoning": context.get("kb_reasoning") or "",
                "customer_cases": context.get("customer_cases") or [],
            })

        if "ai_synthesis" in steps_to_run:
            context, _ = await AISynthesisAgent().safe_run(context)
            await self._checkpoint(case_id, "ai_synthesis", context)
            await self._publish_panel(case_id, "ai_summary", {
                "synthesis": context.get("synthesis") or {},
                "errors": context.get("errors") or {},
            })

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
                    "confidence": synthesis.get("confidence"),
                    "root_cause": synthesis.get("root_cause", "")[:500],
                    "recommended_actions": synthesis.get("recommended_actions", [])[:3],
                    "engineer_summary": synthesis.get("engineer_summary", "")[:500],
                    "status_summary": synthesis.get("status_summary", ""),
                    "updated_at": (context.get("primary_ticket") or {}).get("updated_at", ""),
                    "status": (context.get("primary_ticket") or {}).get("status", ""),
                    "used_fallback": synthesis.get("used_fallback", False),
                    "group_id": context.get("group_id"),
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
                              panel_name: str, data: dict) -> None:
        try:
            from .redis_client import get_redis
            import json
            r = await get_redis()
            message = json.dumps(
                {"panel": panel_name, "data": data})

            # Persist for late WebSocket connections
            await r.setex(
                f"panel:{case_id}:{panel_name}", 3600, message)
            await r.rpush(f"panels:{case_id}", panel_name)
            await r.expire(f"panels:{case_id}", 3600)

            # Publish to live listeners
            await r.publish(f"ws:{case_id}", message)
            log.info("Panel published",
                     case_id=case_id, panel=panel_name)
        except Exception as e:
            log.warning("Panel publish failed",
                        panel=panel_name, error=str(e))

    async def _publish_complete(self, case_id: str,
                                 synthesis: dict,
                                 duration_ms: int) -> None:
        try:
            from .redis_client import get_redis
            import json
            r = await get_redis()
            message = json.dumps({
                "type": "pipeline_complete",
                "case_id": case_id,
                "severity": synthesis.get("unified_severity"),
                "confidence": synthesis.get("confidence"),
                "group_id": synthesis.get("group_id"),
                "duration_ms": duration_ms,
            })

            # Persist for late WebSocket connections
            await r.setex(
                f"panel:{case_id}:pipeline_complete",
                3600, message)
            await r.rpush(
                f"panels:{case_id}", "pipeline_complete")
            await r.expire(f"panels:{case_id}", 3600)

            # Publish to live listeners
            await r.publish(f"ws:{case_id}", message)

        except Exception as e:
            log.warning("publish_complete failed", error=str(e))
