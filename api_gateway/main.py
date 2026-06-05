import asyncio
import re
import time
import uuid
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from .kafka_client import kafka_lifespan
from .routes import auth_router, cases_router, triage_router, settings_router
from .config import ENABLE_LOCAL_PIPELINE_FALLBACK
from .routes.cases_routes import _background_fetch_connector

log = structlog.get_logger()


async def start_kafka_consumer():
    # Only run Kafka consumer if local fallback is disabled
    # Running both causes double pipeline execution
    if ENABLE_LOCAL_PIPELINE_FALLBACK:
        log.info("Local fallback enabled — Kafka consumer "
                 "not started to prevent double execution")
        return
    try:
        from orchestrator.kafka_consumer import consume_triage_requests
        await consume_triage_requests()
    except Exception as e:
        log.warning("kafka_consumer_failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with kafka_lifespan(app):
        consumer_task = asyncio.create_task(start_kafka_consumer())
        app.state.consumer_task = consumer_task
        log.info("gateway_ready", host="0.0.0.0", port=8000)

        # Start background bug list pre-fetcher
        async def background_ingestion():
            while True:
                try:
                    # Acquire Redis lock to prevent duplicate runs (tight TTL of 60 seconds)
                    from orchestrator.redis_client import get_redis
                    redis = await get_redis()
                    lock = await redis.set(
                        "lock:ingest:all", "active",
                        ex=60, nx=True)
                    if not lock:
                        log.info("[Ingestion] Already running, skipping")
                        await asyncio.sleep(10)
                        continue

                    try:
                        from orchestrator.connectors.registry import (
                            ConnectorRegistry)
                        ConnectorRegistry.invalidate_cache()
                        connectors = await ConnectorRegistry.get_all_enabled()
                        excluded = {"confluence", "customer_portal"}

                        fetch_tasks = [
                            _background_fetch_connector(c)
                            for c in connectors
                            if c.system_type not in excluded
                        ]

                        # return_exceptions=True prevents one slow
                        # connector from killing the entire batch
                        results = await asyncio.gather(
                            *fetch_tasks, return_exceptions=True)

                        failed = sum(
                            1 for exc in results
                            if isinstance(exc, Exception))
                        log.info(
                            f"[Ingestion] Complete: "
                            f"{len(fetch_tasks)-failed} ok, "
                            f"{failed} failed")

                    finally:
                        # Always release lock even if ingestion fails
                        try:
                            await redis.delete("lock:ingest:all")
                        except Exception:
                            pass

                except Exception as e:
                    log.warning(f"[Ingestion] Cycle error: {e}")

                # Run every 4 minutes to keep 300s TTL cache warm
                await asyncio.sleep(240)

        ingestion_task = asyncio.create_task(background_ingestion())
        app.state.ingestion_task = ingestion_task
        yield
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
    ingestion_task.cancel()
    try:
        await ingestion_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="HPE Bug Triage API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    request.state.trace_id = trace_id
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "request",
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


app.include_router(auth_router)
app.include_router(cases_router)
app.include_router(triage_router)
app.include_router(settings_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "HPE Bug Triage API"}


@app.get("/mock/confluence/rest/api/content/search")
async def mock_confluence_search(cql: str = "", limit: int = 5):
    """Simulates Confluence CQL search API for POC."""
    from orchestrator.db.session import AsyncSessionLocal
    from orchestrator.db.repositories.kb_articles import search_kb_articles

    match = re.search(r'text[~=]\s*["\']?([^"\'&]+)["\']?', cql)
    query = match.group(1).strip() if match else cql[:50]

    async with AsyncSessionLocal() as db:
        articles = await search_kb_articles(db, query, limit=limit)

    return {
        "results": [
            {
                "id": str(a.id),
                "type": "page",
                "title": a.title,
                "space": {"key": a.space_key},
                "_links": {"webui": a.url},
                "body": {"view": {"value": a.content[:500]}},
                "metadata": {
                    "labels": {"results": [{"name": t} for t in (a.tags or [])]}
                },
                "version": {"when": a.last_modified},
            }
            for a in articles
        ],
        "size": len(articles),
        "limit": limit,
    }


@app.get("/mock/confluence/rest/api/content/{page_id}")
async def mock_confluence_get(page_id: str):
    """Simulates Confluence single-page fetch."""
    from orchestrator.db.session import AsyncSessionLocal
    from orchestrator.db.models import KBArticle
    from sqlalchemy import select

    try:
        pid = int(page_id)
    except ValueError:
        return {}

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(KBArticle).where(KBArticle.id == pid))
        article = result.scalar_one_or_none()

    if not article:
        return {}
    return {
        "id": str(article.id),
        "type": "page",
        "title": article.title,
        "space": {"key": article.space_key},
        "_links": {"webui": article.url},
    }


@app.get("/mock/customer-portal/cases")
async def mock_customer_cases(bug_keywords: str = "", limit: int = 3):
    """Simulates HPE Customer Portal API — returns customer cases by bug keywords."""
    from orchestrator.db.session import AsyncSessionLocal
    from orchestrator.db.models import CustomerCase
    from sqlalchemy import select

    keywords = [k.strip().lower() for k in bug_keywords.split(",") if k.strip()]

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CustomerCase))
        all_cases = result.scalars().all()

    matched = []
    for case in all_cases:
        case_keywords = [k.lower() for k in (case.related_bug_keywords or [])]
        if not keywords or any(k in case_keywords for k in keywords):
            matched.append(case)

    return {
        "cases": [
            {
                "case_id": c.case_id,
                "customer": c.customer,
                "severity": c.severity,
                "title": c.title,
                "impact": c.impact or "",
                "opened_at": c.opened_at.isoformat() if c.opened_at else "",
                "status": c.status,
            }
            for c in matched[:limit]
        ],
        "total": len(matched),
    }
