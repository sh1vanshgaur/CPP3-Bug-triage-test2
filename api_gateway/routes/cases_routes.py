import asyncio
import dataclasses
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from ..auth import get_current_user, User
from orchestrator.connectors.registry import ConnectorRegistry
from orchestrator.redis_client import get_cached_buglist, cache_buglist
from orchestrator.db.session import AsyncSessionLocal
from orchestrator.db.models import (
    AuditLog, SystemGroupRegistry, BugGroupMapping,
)
from orchestrator.db.repositories.audit_log import (
    get_last_triage_for_bug, get_metrics_summary,
    list_recent_pipeline_completions,
)

router = APIRouter(tags=["cases"])

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "Unknown": 4}
_BUG_SOURCE_TYPES = {"github", "jira", "jira_apache", "bugzilla"}


# ── Group assembly ────────────────────────────────────────────────

async def assemble_grouped_bug_list(
        raw_bugs: list[dict],
        db: AsyncSession) -> dict:
    """
    Transform a flat bug page into a tree of groups + standalones.
    All DB lookups are single IN-queries — no N+1 queries.
    """
    all_ticket_ids: set[str] = {
        b["ticket_id"] for b in raw_bugs if b.get("ticket_id")
    }

    # Step 2: batch group-mapping lookup
    group_map: dict[str, str] = {}        # raw_ticket_id → group_id
    if all_ticket_ids:
        result = await db.execute(
            select(
                BugGroupMapping.raw_ticket_id,
                BugGroupMapping.group_id,
            ).where(BugGroupMapping.raw_ticket_id.in_(
                list(all_ticket_ids)))
        )
        for row in result.all():
            group_map[row.raw_ticket_id] = row.group_id

    # Step 3: batch group-info lookup
    group_info: dict[str, dict] = {}      # group_id → metadata
    group_ids = set(group_map.values())
    if group_ids:
        result = await db.execute(
            select(SystemGroupRegistry).where(
                SystemGroupRegistry.group_id.in_(
                    list(group_ids)))
        )
        for grp in result.scalars().all():
            group_info[grp.group_id] = {
                "priority":  grp.priority or "Unknown",
                "status":    grp.status   or "active",
                "title":     grp.title    or "",
                "created_at": (
                    grp.created_at.isoformat()
                    if grp.created_at else ""),
            }

    # Step 4: batch triage-info lookup
    triage_map: dict[str, dict] = {}
    if all_ticket_ids:
        result = await db.execute(
            select(AuditLog)
            .where(
                AuditLog.bug_id.in_(list(all_ticket_ids)),
                AuditLog.step == "pipeline_complete",
            )
            .order_by(AuditLog.bug_id, desc(AuditLog.created_at))
        )
        seen: set[str] = set()
        for entry in result.scalars().all():
            if entry.bug_id not in seen:
                seen.add(entry.bug_id)
                triage_map[entry.bug_id] = {
                    "case_id":   entry.case_id or "",
                    "severity":  (
                        (entry.summary or {}).get("severity")
                        or (entry.summary or {}).get(
                            "unified_severity", "")),
                    "confidence": (
                        (entry.summary or {}).get(
                            "confidence", 0)),
                    "triaged_at": (
                        entry.created_at.isoformat()
                        if entry.created_at else ""),
                    "systems_queried": (
                        entry.systems_queried or []),
                    "duration_ms": entry.duration_ms or 0,
                }

    # Step 5: split grouped vs. ungrouped
    grouped_bugs   = [
        b for b in raw_bugs
        if b.get("ticket_id", "") in group_map
    ]
    ungrouped_bugs = [
        b for b in raw_bugs
        if b.get("ticket_id", "") not in group_map
    ]

    # Step 6: build group parent rows
    by_group: dict[str, list] = defaultdict(list)
    for bug in grouped_bugs:
        by_group[group_map[bug["ticket_id"]]].append(bug)

    _PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "Unknown": 4}
    group_rows = []
    for gid, children in by_group.items():
        info = group_info.get(gid, {})
        child_rows = []
        for child in children:
            tid = child.get("ticket_id", "")
            c   = dict(child)
            c["triage_info"] = triage_map.get(tid)
            c["is_triaged"]  = tid in triage_map
            child_rows.append(c)
        primary_tid = (
            children[0].get("ticket_id", "") if children else "")
        group_rows.append({
            "group_id":   gid,
            "type":       "group",
            "priority":   info.get("priority", "Unknown"),
            "status":     info.get("status", "active"),
            "title":      info.get("title", ""),
            "created_at": info.get("created_at", ""),
            "child_count": len(children),
            "children":   child_rows,
            "triage_info": triage_map.get(primary_tid),
        })
    group_rows.sort(key=lambda g: (
        _PRIO.get(g["priority"], 4),
        g["created_at"],
    ))

    # Step 7: build standalone rows
    standalone_rows = []
    for bug in ungrouped_bugs:
        tid = bug.get("ticket_id", "")
        b   = dict(bug)
        b["type"]       = "standalone"
        b["is_triaged"] = tid in triage_map
        b["triage_info"] = triage_map.get(tid)
        standalone_rows.append(b)

    return {"ungrouped": standalone_rows, "groups": group_rows}


# ── Background helpers ────────────────────────────────────────────

async def background_full_fetch(connector_list: list) -> None:
    for connector in connector_list:
        if connector.system_type not in _BUG_SOURCE_TYPES:
            continue
        try:
            existing = await get_cached_buglist(
                connector.source_id, "open", "")
            if existing and len(existing) > 50:
                continue

            all_tickets = []
            if connector.system_type == "github":
                for pg in range(1, 6):
                    batch = await asyncio.wait_for(
                        connector.search(
                            "", max_results=100, page=pg),
                        timeout=12.0,
                    )
                    if not batch:
                        break
                    all_tickets.extend(batch)
                    if len(batch) < 100:
                        break
                    await asyncio.sleep(0.5)
            elif connector.system_type in ("jira", "jira_apache"):
                for start_at in range(0, 300, 50):
                    batch = await asyncio.wait_for(
                        connector.search(
                            "", max_results=50,
                            start_at=start_at),
                        timeout=12.0,
                    )
                    if not batch:
                        break
                    all_tickets.extend(batch)
                    if len(batch) < 50:
                        break
                    await asyncio.sleep(0.5)
            elif connector.system_type == "bugzilla":
                for offset in range(0, 2000, 500):
                    batch = await asyncio.wait_for(
                        connector.search(
                            "", max_results=500, offset=offset),
                        timeout=15.0,
                    )
                    if not batch:
                        break
                    all_tickets.extend(batch)
                    if len(batch) < 500:
                        break
                    await asyncio.sleep(0.5)

            if all_tickets:
                data = [dataclasses.asdict(t)
                        for t in all_tickets]
                await cache_buglist(
                    connector.source_id, "open", "",
                    data, ttl=300)
                print(
                    f"[BackgroundFetch] {connector.source_id}: "
                    f"{len(data)} bugs cached", flush=True)
        except Exception as e:
            print(
                f"[BackgroundFetch] {connector.source_id} "
                f"failed: {type(e).__name__}: {str(e)[:80]}",
                flush=True)


@router.get("/debug/confluence-test")
async def debug_confluence(q: str = "NormalizeCTEIds"):
    import asyncio
    from orchestrator.connectors.registry import ConnectorRegistry

    connectors = await ConnectorRegistry.get_all_enabled()
    conf = next(
        (c for c in connectors if c.system_type == "confluence"),
        None)

    if not conf:
        return {"error": "No confluence connector found"}

    try:
        results = await asyncio.wait_for(
            conf.search(q, max_results=5), timeout=15.0)
        return {
            "connector":     conf.source_id,
            "base_url":      conf.base_url,
            "query":         q,
            "results_count": len(results),
            "titles":        [r.title for r in results],
            "urls":          [r.url for r in results],
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/debug/sources")
async def debug_sources():
    from orchestrator.db.session import AsyncSessionLocal
    from orchestrator.db.repositories.source_registry import (
        get_all_sources)
    from orchestrator.connectors.registry import (
        ConnectorRegistry, load_connectors_from_db)
    import os

    async with AsyncSessionLocal() as db:
        sources = await get_all_sources(db)

    connectors = await load_connectors_from_db()

    return {
        "db_sources": [
            {
                "source_id":      s.source_id,
                "system_type":    s.system_type,
                "enabled":        s.enabled,
                "auth_secret_ref": s.auth_secret_ref,
                "token_present":  bool(
                    os.environ.get(s.auth_secret_ref or "", "")),
                "project_key":    s.project_key,
            }
            for s in sources
        ],
        "connectors_loaded": len(connectors),
        "connector_ids": [c.source_id for c in connectors],
    }


def get_bug_score(severity: str, updated_at: str) -> float:
    sev_val = {
        "P0": 4, "P1": 3, "P2": 2, "P3": 1
    }.get(severity or "Unknown", 0)
    ts = 0.0
    if updated_at:
        try:
            s = str(updated_at).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if len(s) > 5 and s[-5] in ('+', '-'):
                s = s[:-2] + ":" + s[-2:]
            dt = datetime.fromisoformat(s)
            ts = dt.timestamp()
        except Exception:
            pass
    return sev_val * 10 + (ts / 2000000000.0)


async def _background_fetch_connector(connector) -> None:
    try:
        connector_class = type(connector).__name__.lower()
        tickets = []

        if "jira" in connector_class:
            for start_at in [0, 100, 200]:
                try:
                    batch = await asyncio.wait_for(
                        connector.search(
                            "", max_results=100,
                            start_at=start_at),
                        timeout=8.0)
                    if not batch:
                        break
                    tickets.extend(batch)
                    if len(batch) < 100:
                        break
                except (asyncio.TimeoutError, Exception):
                    break

        elif "github" in connector_class:
            for page in [1, 2, 3]:
                try:
                    batch = await asyncio.wait_for(
                        connector.search(
                            "", max_results=100, page=page),
                        timeout=8.0)
                    if not batch:
                        break
                    tickets.extend(batch)
                    if len(batch) < 100:
                        break
                except (asyncio.TimeoutError, Exception):
                    break

        elif "bugzilla" in connector_class:
            try:
                tickets = await asyncio.wait_for(
                    connector.search("", max_results=300),
                    timeout=8.0)
            except (asyncio.TimeoutError, Exception):
                tickets = []

        else:
            try:
                tickets = await asyncio.wait_for(
                    connector.search("", max_results=50),
                    timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                tickets = []

        if tickets:
            data = [dataclasses.asdict(t) for t in tickets]
            await cache_buglist(
                connector.source_id, "open", "",
                data, ttl=120)

            from orchestrator.redis_client import get_redis
            r = await get_redis()
            for ticket in tickets:
                bug_id = ticket.ticket_id
                t_dict = dataclasses.asdict(ticket)
                await r.hset(
                    f"bug:data:{bug_id}", "data",
                    json.dumps(t_dict))
                await r.expire(f"bug:data:{bug_id}", 120)

                score = get_bug_score(
                    ticket.severity, ticket.updated_at)
                await r.zadd(
                    "buglist:all:scores", {bug_id: score})

            await r.expire("buglist:all:scores", 120)

            print(f"[BugList] {connector.source_id}: "
                  f"{len(data)} bugs cached")

    except Exception as e:
        print(f"[BugList] {connector.source_id} "
              f"background fetch error: {e}")


# ── GET /bugs ─────────────────────────────────────────────────────

@router.get("/bugs")
async def get_bugs(
    page:       int = Query(1, ge=1),
    page_size:  int = Query(50, ge=1, le=200),
    search:     str = Query(""),
    severity:   str = Query(""),
    source:     str = Query(""),
    status:     str = Query(""),
    sort_field: str = Query("severity"),
    sort_order: str = Query("desc"),
    user: User = Depends(get_current_user),
):
    all_connectors = await ConnectorRegistry.get_all_enabled()
    connectors = [
        c for c in all_connectors
        if c.system_type in _BUG_SOURCE_TYPES
    ]

    if not connectors:
        return {
            "ungrouped": [], "groups": [],
            "total": 0, "page": page,
            "page_size": page_size, "sources_online": 0,
            "sources_total": 0, "partial": False,
            "message": "No connectors configured",
        }

    from orchestrator.redis_client import get_redis
    r = await get_redis()

    # Assembled-list cache (TTL 30 s, keyed on all filter dims)
    cache_key = (
        f"bug_list:{page}:{page_size}:{sort_field}:{sort_order}"
        f":{search}:{severity}:{source}:{status}"
    )
    try:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    # Fetch bug IDs from ZSET then bulk-fetch data hashes
    bug_ids = await r.zrevrange("buglist:all:scores", 0, -1)

    all_bugs = []
    if bug_ids:
        pipe = r.pipeline()
        for bid in bug_ids:
            pipe.hget(f"bug:data:{bid}", "data")
        results = await pipe.execute()
        for res in results:
            if res:
                try:
                    all_bugs.append(json.loads(res))
                except Exception:
                    pass

    sources_online = len(connectors)

    # Filters
    if search:
        sl = search.strip().lower()

        def matches(b: dict) -> bool:
            if sl in (b.get("ticket_id") or "").lower():
                return True
            if sl in (b.get("title") or "").lower():
                return True
            if sl in (b.get("component") or "").lower():
                return True
            if sl in (b.get("source_id") or "").lower():
                return True
            if sl in (b.get("system_type") or "").lower():
                return True
            if sl in (b.get("severity") or "").lower():
                return True
            if sl in (b.get("status") or "").lower():
                return True
            desc = (b.get("description") or "")[:200].lower()
            if sl in desc:
                return True
            for label in (b.get("labels") or []):
                if sl in str(label).lower():
                    return True
            return False

        all_bugs = [b for b in all_bugs if matches(b)]
    if severity:
        all_bugs = [
            b for b in all_bugs
            if b.get("severity", "") == severity
        ]
    if source:
        all_bugs = [
            b for b in all_bugs
            if b.get("source_id", "") == source
        ]
    if status:
        all_bugs = [
            b for b in all_bugs
            if b.get("status", "").lower() == status.lower()
        ]

    all_bugs.sort(
        key=lambda b: SEVERITY_ORDER.get(
            b.get("severity", "Unknown"), 4))
    total     = len(all_bugs)
    start_idx = (page - 1) * page_size
    page_bugs = all_bugs[start_idx: start_idx + page_size]

    # Assemble grouped tree (all DB lookups are IN-queries)
    assembled: dict = {"ungrouped": [], "groups": []}
    try:
        async with AsyncSessionLocal() as db:
            assembled = await assemble_grouped_bug_list(
                raw_bugs=page_bugs, db=db)
    except Exception as e:
        print(f"[BugList] assemble_grouped_bug_list failed: {e}",
              flush=True)
        for bug in page_bugs:
            bug["type"]       = "standalone"
            bug["is_triaged"] = False
            bug["triage_info"] = None
        assembled = {"ungrouped": page_bugs, "groups": []}

    response = {
        **assembled,
        "total":         total,
        "page":          page,
        "page_size":     page_size,
        "sources_online": sources_online,
        "sources_total": len(connectors),
        "partial":       False,
    }

    # Cache assembled response (TTL 30 s)
    try:
        await r.setex(
            cache_key, 30,
            json.dumps(response, default=str))
    except Exception:
        pass

    return response


@router.post("/bugs/warm")
async def warm_bug_cache(user: User = Depends(get_current_user)):
    connectors = await ConnectorRegistry.get_all_enabled()
    asyncio.create_task(background_full_fetch(connectors))
    return {
        "status":  "warming",
        "connectors": len(connectors),
        "message": (
            f"Cache warming started for {len(connectors)} "
            f"connectors in background"),
    }


@router.post("/bugs/refresh")
async def refresh_bugs(user: User = Depends(get_current_user)):
    from orchestrator.redis_client import purge_buglist_cache
    cleared = await purge_buglist_cache()
    return {
        "cleared_keys": cleared,
        "message": (
            "Bug list cache cleared. "
            "Next GET /bugs will fetch fresh data."),
    }


@router.get("/bugs/{bug_id}/status")
async def get_bug_status(
    bug_id: str,
    user: User = Depends(get_current_user),
):
    # Step 1: Fetch last audit record
    async with AsyncSessionLocal() as db:
        last_triage = await get_last_triage_for_bug(db, bug_id)

    if not last_triage:
        return {
            "is_new":           True,
            "needs_retriage":   True,
            "changes":          [],
            "last_triaged_at":  None,
            "last_severity":    None,
            "last_confidence":  None,
        }

    summary           = last_triage.summary or {}
    ticket_updated_at = summary.get("updated_at", "")
    last_severity     = summary.get("severity", "")
    last_status       = summary.get("status", "")
    last_confidence   = summary.get("confidence", 0)
    last_triaged_at   = (
        last_triage.created_at.isoformat()
        if last_triage.created_at else None
    )

    # Step 1b: Resolve connector
    connector = None
    try:
        connector = await ConnectorRegistry.get_connector(
            last_triage.source_id or "")
    except Exception:
        pass

    if not connector:
        try:
            connectors  = await ConnectorRegistry.get_all_enabled()
            bug_upper   = bug_id.upper()
            sorted_c    = sorted(
                connectors,
                key=lambda c: len(c.ticket_prefix or ""),
                reverse=True)
            for c in sorted_c:
                prefix = (c.ticket_prefix or "").upper().strip()
                if prefix and bug_upper.startswith(prefix):
                    connector = c
                    break
        except Exception:
            pass

    if not connector:
        return {
            "is_new":           False,
            "last_triaged_at":  last_triaged_at,
            "last_severity":    last_severity,
            "last_confidence":  last_confidence,
            "changes":          [],
            "needs_retriage":   False,
        }

    # Step 2: Lightweight freshness check
    live = {}
    try:
        live = await asyncio.wait_for(
            connector.get_lightweight(bug_id),
            timeout=8.0)
    except Exception as e:
        import structlog
        structlog.get_logger().warning(
            "get_lightweight failed",
            bug_id=bug_id, error=str(e))

    if not live:
        return {
            "is_new":           False,
            "last_triaged_at":  last_triaged_at,
            "last_severity":    last_severity,
            "last_confidence":  last_confidence,
            "changes":          [],
            "needs_retriage":   False,
        }

    # Step 3: Decide path
    live_updated_at = live.get("updated_at", "")
    live_severity   = live.get("severity", "")
    live_status     = live.get("status", "")

    def to_datetime(val) -> datetime:
        if isinstance(val, datetime):
            return val
        if not val:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            s = str(val).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if len(s) > 5 and s[-5] in ('+', '-'):
                s = s[:-2] + ":" + s[-2:]
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    last_triaged_dt = to_datetime(last_triage.created_at)
    live_updated_dt = to_datetime(live_updated_at)

    no_change = (
        live_updated_dt <= last_triaged_dt
        and live_severity == last_severity
        and live_status   == last_status
    )

    if no_change:
        return {
            "is_new":           False,
            "last_triaged_at":  last_triaged_at,
            "last_severity":    last_severity,
            "last_confidence":  last_confidence,
            "changes":          [],
            "needs_retriage":   False,
        }

    # Step 4: Change detected — fetch detailed changelog
    changelog = []
    try:
        changelog = await asyncio.wait_for(
            connector.get_changelog(
                bug_id, since=last_triaged_at),
            timeout=10.0)
    except Exception:
        pass

    # Step 5: Filter and build response
    relevant = {
        "priority", "status", "severity",
        "assignee", "resolution", "description"
    }
    changes = [
        {
            "field":      e.field,
            "from":       e.old_value,
            "to":         e.new_value,
            "changed_at": e.changed_at,
            "changed_by": e.changed_by,
        }
        for e in changelog
        if e.field.lower() in relevant
    ]

    if not changes:
        if live_severity and live_severity != last_severity:
            changes.append({
                "field":      "severity",
                "from":       last_severity,
                "to":         live_severity,
                "changed_at": live_updated_at,
                "changed_by": "",
            })
        if live_status and live_status != last_status:
            changes.append({
                "field":      "status",
                "from":       last_status,
                "to":         live_status,
                "changed_at": live_updated_at,
                "changed_by": "",
            })

    return {
        "is_new":           False,
        "last_triaged_at":  last_triaged_at,
        "last_severity":    last_severity,
        "last_confidence":  last_confidence,
        "changes":          changes,
        "needs_retriage":   True,
    }


@router.get("/metrics")
async def get_metrics(user: User = Depends(get_current_user)):
    async with AsyncSessionLocal() as db:
        summary = await get_metrics_summary(db)
        recent  = await list_recent_pipeline_completions(
            db, limit=10)

    all_connectors = await ConnectorRegistry.get_all_enabled()
    bug_connectors = [
        c for c in all_connectors
        if c.system_type in _BUG_SOURCE_TYPES
    ]

    by_severity: dict[str, int] = {
        "P0": 0, "P1": 0, "P2": 0, "P3": 0, "Unknown": 0}
    source_counts: dict[str, int] = {}
    total_confidence = 0.0
    confidence_count = 0

    for entry in recent:
        s = (
            (entry.summary or {}).get("unified_severity")
            or (entry.summary or {}).get("severity", "Unknown")
        )
        if s not in by_severity:
            s = "Unknown"
        by_severity[s] += 1
        src = entry.source_id or "unknown"
        source_counts[src] = source_counts.get(src, 0) + 1
        conf = (entry.summary or {}).get("confidence", 0)
        if conf:
            total_confidence += conf
            confidence_count += 1

    avg_confidence = (
        round(total_confidence / confidence_count, 2)
        if confidence_count else 0)

    # Live P0/P1 counts from Redis-cached bug data
    live_p0 = 0
    live_p1 = 0
    live_total = 0
    try:
        for connector in bug_connectors:
            cached = await get_cached_buglist(
                connector.source_id, "open", "")
            if cached:
                for bug in cached:
                    live_total += 1
                    sev = bug.get("severity", "Unknown")
                    if sev == "P0":
                        live_p0 += 1
                    elif sev == "P1":
                        live_p1 += 1
    except Exception:
        pass

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    triaged_today = sum(
        1 for e in recent
        if e.created_at and e.created_at >= today_start
    )

    total_triages = summary.get("total_triaged", 0)
    needs_triage  = max(0, live_total - total_triages)

    return {
        "total_triages":   total_triages,
        "total_triaged":   total_triages,
        "sources_online":  len(bug_connectors),
        "sources_total":   len(bug_connectors),
        "by_severity":     by_severity,
        "by_source":       source_counts,
        "avg_confidence":  avg_confidence,
        "live_p0_count":   live_p0,
        "live_p1_count":   live_p1,
        "live_total_bugs": live_total,
        "triaged_today":   triaged_today,
        "needs_triage":    needs_triage,
        "recent_activity": [
            {
                "case_id":    e.case_id or "",
                "bug_id":     e.bug_id,
                "source_id":  e.source_id or "",
                "severity":   (
                    (e.summary or {}).get("unified_severity")
                    or (e.summary or {}).get(
                        "severity", "Unknown")),
                "confidence": (
                    (e.summary or {}).get("confidence", 0)),
                "root_cause": (
                    ((e.summary or {}).get(
                        "root_cause") or "")[:100]),
                "duration_ms": e.duration_ms or 0,
                "engineer_id": e.engineer_id or "",
                "created_at":  (
                    e.created_at.isoformat()
                    if e.created_at else ""),
            }
            for e in recent
        ],
    }


@router.get("/history/triage")
async def get_triage_history(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    async with AsyncSessionLocal() as db:
        entries = await list_recent_pipeline_completions(
            db, limit=limit)

    results = []
    for e in entries:
        summary = e.summary or {}
        results.append({
            "id":              e.id,
            "case_id":         e.case_id or "",
            "bug_id":          e.bug_id,
            "source_id":       e.source_id or "",
            "engineer_id":     e.engineer_id or "",
            "severity":        (
                summary.get("severity")
                or summary.get(
                    "unified_severity", "Unknown")),
            "confidence":      summary.get("confidence", 0),
            "root_cause":      (
                summary.get("root_cause") or "")[:120],
            "duration_ms":     e.duration_ms or 0,
            "systems_queried": e.systems_queried or [],
            "triaged_at":      (
                e.created_at.isoformat()
                if e.created_at else None),
        })
    return results


@router.get("/cases/{case_id}")
async def get_case_result(
    case_id: str,
    user: User = Depends(get_current_user),
):
    from fastapi import HTTPException
    from orchestrator.redis_client import get_cached_case_result
    cached = await get_cached_case_result(case_id)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail=(
                "Case result not found. "
                "Results are cached for 1 hour after triage."),
        )
    return cached
