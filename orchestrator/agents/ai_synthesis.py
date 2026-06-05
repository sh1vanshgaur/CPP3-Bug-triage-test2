import json
import os
import structlog
from groq import AsyncGroq
from pydantic import ValidationError
from .base import BaseAgent
from ..models.synthesis import SynthesisOutput
from ..db.session import AsyncSessionLocal
from ..db.repositories.group_registry import (
    get_next_group_id, create_group,
    get_group_for_any_ticket, add_tickets_to_group
)

log = structlog.get_logger()

SYNTHESIS_SCHEMA = """{
  "unified_severity": "P0|P1|P2|P3",
  "status_summary": "string",
  "affected_components": ["string"],
  "root_cause": "string",
  "recommended_actions": ["string"],
  "engineer_summary": "string",
  "customer_summary": "string",
  "confidence": 0.0-1.0,
  "reasoning": "string",
  "used_fallback": false
}"""


class AISynthesisAgent(BaseAgent):
    step_name = "ai_synthesis"

    async def run(self, context: dict) -> dict:
        primary = context.get("primary_ticket") or {}
        related = context.get("related_tickets") or []
        kb_articles = context.get("kb_articles") or []
        customer_cases = context.get("customer_cases") or []
        print(f"[AISynthesis] Starting with {len(related)} related tickets and {len(kb_articles)} KB articles", flush=True)

        groq_api_key = os.getenv("GROQ_API_KEY", "")
        groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

        if not groq_api_key:
            context["synthesis"] = self._keyword_fallback(primary).model_dump()
            return context

        # BUG2-B: cache check — same bug always returns same triage within 1 hour
        bug_id_ctx    = context.get("bug_id", "")
        source_id_ctx = context.get("source_id", "")
        force_refresh = context.get("force_refresh", False)
        _cache_key    = f"triage_result:{source_id_ctx}:{bug_id_ctx}"
        if not force_refresh and bug_id_ctx:
            try:
                from ..redis_client import get_redis
                _r = await get_redis()
                _cached = await _r.get(_cache_key)
                if _cached:
                    log.info("AISynthesis: cache hit", bug_id=bug_id_ctx)
                    context["synthesis"] = json.loads(_cached)
                    try:
                        _s = SynthesisOutput(**context["synthesis"])
                        _gid = await self._resolve_group_id(context, _s)
                        if _gid:
                            context["group_id"] = _gid
                            context["synthesis"]["group_id"] = _gid
                    except Exception:
                        pass
                    return context
            except Exception as e:
                log.warning("AISynthesis: cache check failed", error=str(e))

        prompt = self._build_prompt(primary, related, kb_articles, customer_cases)
        client = AsyncGroq(api_key=groq_api_key)

        synthesis = None
        for attempt in range(2):
            try:
                extra = ""
                if attempt == 1:
                    extra = f"\n\nIMPORTANT: Respond ONLY with valid JSON matching this schema:\n{SYNTHESIS_SCHEMA}"
                resp = await client.chat.completions.create(
                    model=groq_model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Always respond with a single definitive answer. "
                                "Do not hedge or say 'insufficient information'. "
                                "If data is limited, make your best determination "
                                "and state your confidence level numerically."
                            ),
                        },
                        {"role": "user", "content": prompt + extra},
                    ],
                    temperature=0.0,
                    seed=42,
                    response_format={"type": "json_object"},
                    max_tokens=1024,
                )
                raw = resp.choices[0].message.content or "{}"
                data = json.loads(raw)
                # BUG1: normalize confidence before Pydantic validates ge=0 le=1
                raw_conf = data.get("confidence")
                if isinstance(raw_conf, (int, float)):
                    if raw_conf > 1.0:
                        data["confidence"] = round(float(raw_conf) / 10.0, 2)
                    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
                synthesis = SynthesisOutput(**data)
                break
            except (ValidationError, json.JSONDecodeError, Exception) as e:
                log.warning("Synthesis attempt failed", attempt=attempt, error=str(e))

        # If both attempts failed, try JSON repair with fast model
        if synthesis is None and groq_api_key:
            try:
                repair_client = AsyncGroq(api_key=groq_api_key)
                repair_prompt = (
                    f"The following text should be valid JSON matching "
                    f"this schema:\n{SYNTHESIS_SCHEMA}\n\n"
                    f"Fix any syntax errors and return only valid JSON:\n"
                    f"{prompt[:500]}"
                )
                repair_resp = await repair_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": repair_prompt}],
                    temperature=0.0,
                    seed=42,
                    response_format={"type": "json_object"},
                    max_tokens=512,
                )
                raw = repair_resp.choices[0].message.content or "{}"
                data = json.loads(raw)
                raw_conf = data.get("confidence")
                if isinstance(raw_conf, (int, float)):
                    if raw_conf > 1.0:
                        data["confidence"] = round(float(raw_conf) / 10.0, 2)
                    data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
                synthesis = SynthesisOutput(**data)
                log.info("Synthesis repaired with fast model")
            except Exception as e:
                log.warning("Synthesis repair also failed", error=str(e))

        if synthesis is None:
            synthesis = self._keyword_fallback(primary)

        context["synthesis"] = synthesis.model_dump()

        # BUG2-B: persist synthesis result so re-triage within 1 h returns same scores
        if bug_id_ctx:
            try:
                from ..redis_client import get_redis
                _r = await get_redis()
                await _r.setex(_cache_key, 3600,
                               json.dumps(context["synthesis"]))
            except Exception:
                pass

        # BT-xxx System ID Resolution Block
        group_id = await self._resolve_group_id(
            context, synthesis)
        if group_id:
            context["group_id"] = group_id
            context["synthesis"]["group_id"] = group_id
            log.info("BT group resolved",
                     case_id=context.get("case_id"),
                     group_id=group_id)

        return context

    def _build_prompt(self, primary: dict, related: list,
                      kb_articles: list, customer_cases: list = None) -> str:
        related_str = ""
        for r in related[:5]:
            src = r.get('source') or r.get('source_id') or r.get('system_type') or ''
            tid = r.get('id') or r.get('ticket_id') or ''
            related_str += f"- [{src}] {tid} — {r.get('title','')} (score: {r.get('similarity_score', 0):.2f}, reason: {r.get('similarity_reason','')})\n"

        kb_str = ""
        for kb in kb_articles[:3]:
            kb_str += f"- {kb.get('title','')} ({kb.get('relevance','')}) — {kb.get('excerpt','')[:100]}\n"

        customer_str = ""
        for cc in (customer_cases or [])[:3]:
            customer_str += (
                f"- [{cc.get('case_id','')}] {cc.get('customer','')} — "
                f"{cc.get('title','')} "
                f"(severity: {cc.get('severity','')}, "
                f"impact: {cc.get('impact','')[:80]})\n"
            )

        return f"""You are an expert software bug triage system. Analyze the following bug and produce a comprehensive triage report.

PRIMARY BUG:
ID: {primary.get('ticket_id','')}
Title: {primary.get('title','')}
Severity: {primary.get('severity','')}
Status: {primary.get('status','')}
Component: {primary.get('component','')}
Reporter: {primary.get('reporter','')}
Assignee: {primary.get('assignee','')}
Description: {(primary.get('description') or '')[:600]}

RELATED ISSUES FOUND:
{related_str or "None found"}

KNOWLEDGE BASE ARTICLES:
{kb_str or "None found"}

CUSTOMER CASES REPORTING THIS BUG:
{customer_str or "None reported"}

Respond with a JSON object matching this schema exactly:
{SYNTHESIS_SCHEMA}

Guidelines:
- unified_severity: determine based on impact, affected components, and related issues
- confidence: 0.9+ if you have clear evidence, 0.6-0.9 if moderate evidence, <0.6 if uncertain
- engineer_summary: technical details for the engineer
- customer_summary: non-technical explanation for the customer
- recommended_actions: 3-5 specific actionable steps"""

    async def _resolve_group_id(self, context: dict,
                                 synthesis) -> str | None:
        primary = context.get("primary_ticket") or {}
        related = context.get("related_tickets") or []

        primary_ticket_ref = {
            "ticket_id": primary.get("ticket_id", ""),
            "source_id": primary.get("source_id",
                                      context.get("source_id", "")),
            "system_type": primary.get("system_type", ""),
        }

        all_tickets = [primary_ticket_ref] + [
            {
                "ticket_id":  t.get("ticket_id") or t.get("id") or "",
                "source_id":  t.get("source_id") or t.get("source") or "",
                "system_type": t.get("system_type") or t.get("source") or "",
            }
            for t in related
            if t.get("similarity_score", 0) >= 0.50
        ]

        try:
            async with AsyncSessionLocal() as db:
                # Scenario A: join existing group
                existing = await get_group_for_any_ticket(
                    db, all_tickets)
                if existing:
                    await add_tickets_to_group(
                        db, existing, all_tickets)
                    return existing

                # Scenario B: mint new group
                new_id = await get_next_group_id(db)
                priority = getattr(synthesis,
                                    "unified_severity", "P2")
                title = primary.get("title", "Bug Group")[:300]
                src = primary.get(
                    "source_id", context.get("source_id", ""))
                await create_group(db, new_id, title,
                                   priority, src)
                await add_tickets_to_group(
                    db, new_id, all_tickets)
                return new_id

        except Exception as e:
            log.warning("BT group resolution failed",
                        error=str(e))
            return None

    def _keyword_fallback(self, primary: dict) -> SynthesisOutput:
        severity = primary.get("severity", "P2")
        title = primary.get("title", "Bug")
        component = primary.get("component", "Unknown")
        return SynthesisOutput(
            unified_severity=severity if severity in ("P0", "P1", "P2", "P3") else "P2",
            status_summary=f"Bug '{title}' requires investigation.",
            affected_components=[component] if component else [],
            root_cause="Root cause analysis requires manual investigation.",
            recommended_actions=[
                "Reproduce the issue in a controlled environment",
                "Check recent commits touching the affected component",
                "Review logs around the time of failure",
            ],
            engineer_summary=f"Technical investigation needed for {title} in component {component}.",
            customer_summary="Our team is investigating this issue and will provide updates shortly.",
            confidence=0.3,
            reasoning="Fallback analysis — Groq synthesis unavailable.",
            used_fallback=True,
        )
