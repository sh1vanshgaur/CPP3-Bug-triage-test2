import asyncio
import dataclasses
import os
import json
import structlog
from groq import AsyncGroq
from .base import BaseAgent
from ..connectors.registry import ConnectorRegistry
from ..models.synthesis import CandidateScore
from ..utils.url_utils import sanitize_bug_url

log = structlog.get_logger()

STOP_WORDS = {
    "a", "an", "the", "to", "with", "when", "in", "on", "at", "by",
    "for", "of", "and", "or", "is", "it", "this", "that", "was",
    "are", "be", "as", "from", "but", "not", "have", "has", "had",
    "he", "she", "they", "we", "you", "i", "its", "if", "so", "do",
    "did", "will", "would", "could", "should", "may", "might", "can",
    "error", "issue", "bug", "fix", "update", "add", "remove", "change",
}


class CrossSystemFetchAgent(BaseAgent):
    step_name = "cross_system_fetch"

    async def run(self, context: dict) -> dict:
        primary        = context.get("primary_ticket") or {}
        primary_source = context.get("source_id", "")

        if not primary or not primary.get("title"):
            log.warning("CrossSystem: no primary ticket or title")
            context["related_tickets"]    = []
            context["sources_queried"]    = []
            context["related_candidates"] = []
            return context

        description = primary.get("description", "")
        comments    = primary.get("comments", [])
        title       = primary.get("title", "")
        component   = primary.get("component", "")

        # Derive search keywords from title + component
        _stop = {
            "add", "fix", "update", "remove", "missing",
            "error", "failed", "cannot", "unable", "invalid",
            "exception", "issue", "problem", "wrong", "broken",
            "with", "from", "into", "this", "that", "using",
        }
        keywords = list(dict.fromkeys(
            w.strip(".,()[]").lower()
            for w in f"{title} {component}".split()
            if len(w.strip(".,()[]")) > 3
            and w.lower().strip(".,()[]") not in _stop
        ))[:8]

        # Fetch connectors once — used for backlink URL construction and Level 3
        all_connectors = await ConnectorRegistry.get_all_enabled()
        jira_base = next(
            (c.base_url for c in all_connectors
             if "jira" in (c.system_type or "").lower()), "")
        github_base = next(
            (c.base_url for c in all_connectors
             if c.system_type == "github"), "https://api.github.com")
        github_repo = next(
            (c.project_key for c in all_connectors
             if c.system_type == "github"), "")
        bugzilla_base = next(
            (c.base_url for c in all_connectors
             if c.system_type == "bugzilla"), "")

        # LEVEL 1: Backlink extraction with real URLs
        backlinks  = self._extract_backlinks(description, comments)
        candidates = []
        for jira_id in backlinks["jira"]:
            url = f"{jira_base}/browse/{jira_id}" if jira_base else ""
            candidates.append({
                "id":              jira_id,
                "source":          "jira",
                "from_backlink":   True,
                "title":           jira_id,
                "description":     "",
                "url":             url,
                "relevance_score": 0.6,
                "similarity_score": 0.6,
                "similarity_label":  "Referenced",
                "similarity_reason": "Referenced in ticket text (unverified)",
            })
        for gh_id in backlinks["github"]:
            url = (f"https://github.com/{github_repo}/issues/{gh_id}"
                   if github_repo else "")
            candidates.append({
                "id":              f"#{gh_id}",
                "source":          "github",
                "from_backlink":   True,
                "title":           f"GitHub #{gh_id}",
                "description":     "",
                "url":             url,
                "relevance_score": 0.6,
                "similarity_score": 0.6,
                "similarity_label":  "Referenced",
                "similarity_reason": "Referenced in ticket text (unverified)",
            })
        for bz_id in backlinks["bugzilla"]:
            url = (f"{bugzilla_base}/show_bug.cgi?id={bz_id}"
                   if bugzilla_base else "")
            candidates.append({
                "id":              f"BZ-{bz_id}",
                "source":          "bugzilla",
                "from_backlink":   True,
                "title":           f"BZ-{bz_id}",
                "description":     "",
                "url":             url,
                "relevance_score": 0.6,
                "similarity_score": 0.6,
                "similarity_label":  "Referenced",
                "similarity_reason": "Referenced in ticket text (unverified)",
            })
        log.info("CrossSystem backlinks found",
                 jira=len(backlinks["jira"]),
                 github=len(backlinks["github"]),
                 bugzilla=len(backlinks["bugzilla"]))
        context["backlink_candidates"] = backlinks

        # LEVEL 2: Redis cache scan + Groq scoring
        cache_candidates = await self._scan_redis_cache(keywords)
        if cache_candidates:
            source_ticket = {
                "title":       title,
                "description": description,
            }
            scored = await self._batch_score_with_groq(
                source_ticket, cache_candidates)
            candidates.extend(scored)

        # LEVEL 3: Live API search — always run for external systems
        # to ensure cross-system discovery finds bugs from other
        # connected repos, not just the primary source.
        log.info(
            "CrossSystem: running live API search against "
            "external connected systems",
            level2_candidates=len(candidates))
        live_results, sources_queried = await self._live_api_search(
            primary, primary_source, context,
            all_connectors=all_connectors)
        candidates.extend(live_results)

        # Normalize ALL candidates, drop self-matches, and deduplicate by
        # normalized source + ticket id before Panel 2 sees the payload.
        primary_refs = self._primary_refs(primary)
        seen_keys: set[tuple[str, str]] = set()
        normalized: list[dict] = []
        for c in candidates:
            n = self._normalize_candidate(c)
            if self._is_primary_match(n, primary_refs):
                continue
            key = self._dedupe_key(n)
            if key[1] and key not in seen_keys:
                seen_keys.add(key)
                normalized.append(n)

        normalized.sort(
            key=lambda x: x.get("similarity_score", 0.0),
            reverse=True)

        context["related_tickets"]    = normalized
        context["related_issues"]     = {
            "related_tickets": normalized,
            "sources_queried": sources_queried,
        }
        context["sources_queried"]    = sources_queried
        context["related_candidates"] = normalized
        return context

    # ── Candidate normalization ───────────────────────────────────
    def _normalize_candidate(self, raw: dict) -> dict:
        """
        Converts any candidate dict (Level 1 backlinks, Level 2 Redis/Groq,
        or Level 3 live API) into the standard shape the frontend expects:
        id, title, url, status, source, description, relevance_score.
        """
        id_ = (
            raw.get("id") or
            raw.get("ticket_id") or
            raw.get("key") or ""
        )
        source = (
            raw.get("system_type") or
            raw.get("source") or
            raw.get("source_id") or "unknown"
        )
        source_id = raw.get("source_id") or raw.get("source") or source
        # GitHub IDs are plain numbers from the API; prefix with # for display
        if ("github" in (source or "").lower()
                and id_
                and str(id_).isdigit()):
            id_ = f"#{id_}"

        url = (
            raw.get("url") or
            raw.get("html_url") or
            raw.get("link") or ""
        )
        url = sanitize_bug_url(url=str(url), system_type=source, bug_id=id_)

        # Accept either field name for the relevance score
        score_val = raw.get("relevance_score")
        if score_val is None:
            score_val = raw.get("similarity_score")
        raw_score = float(score_val or 0.0)
        # Clamp: LLM may return 0-10 scale; always store as 0.0-1.0
        score = round(max(0.0, min(1.0,
            raw_score / 10.0 if raw_score > 1.0 else raw_score)), 2)

        status = (raw.get("status") or raw.get("state") or "unknown").lower()

        return {
            "id":               id_,
            "ticket_id":        id_,     # backward-compat alias
            "title":            (raw.get("title") or raw.get("summary")
                                 or raw.get("name") or ""),
            "url":              url,
            "status":           status,
            "source":           source,
            "source_id":        source_id,
            "system_type":      source,  # backward-compat alias
            "description":      (raw.get("description") or
                                 raw.get("body") or "")[:300],
            "relevance_score":  score,
            "similarity_score": score,   # alias
            "similarity_label":  (raw.get("similarity_label") or ""),
            "similarity_reason": (raw.get("similarity_reason") or
                                  raw.get("reason") or ""),
            "relationship_type": (raw.get("relationship_type") or
                                  raw.get("relationship") or
                                  raw.get("type") or ""),
            "raw_key":          raw.get("raw_key") or raw.get("key") or "",
        }

    def _normalize_ticket_ref(self, value) -> str:
        text = str(value or "").strip().upper()
        if text.startswith("#"):
            text = text[1:]
        return text

    def _candidate_refs(self, item: dict) -> set[str]:
        return {
            self._normalize_ticket_ref(item.get(key))
            for key in ("ticket_id", "id", "key", "raw_key")
            if self._normalize_ticket_ref(item.get(key))
        }

    def _primary_refs(self, primary: dict) -> dict:
        refs = {
            self._normalize_ticket_ref(primary.get(key))
            for key in ("ticket_id", "id", "key", "raw_key")
            if self._normalize_ticket_ref(primary.get(key))
        }
        source_refs = {
            self._normalize_ticket_ref(primary.get(key))
            for key in ("source_id", "source", "system_type")
            if self._normalize_ticket_ref(primary.get(key))
        }
        return {"ids": refs, "sources": source_refs}

    def _is_primary_match(self, item: dict, primary_refs: dict) -> bool:
        candidate_ids = self._candidate_refs(item)
        if not candidate_ids:
            return False
        if candidate_ids & primary_refs["ids"]:
            return True
        source = self._normalize_ticket_ref(
            item.get("source_id") or item.get("source") or item.get("system_type"))
        return bool(
            source
            and source in primary_refs["sources"]
            and candidate_ids & primary_refs["ids"])

    def _dedupe_key(self, item: dict) -> tuple[str, str]:
        source = self._normalize_ticket_ref(
            item.get("source_id") or item.get("source") or item.get("system_type"))
        refs = sorted(self._candidate_refs(item))
        return source, refs[0] if refs else ""

    # ── Redis cache scan (Level 2) ────────────────────────────────
    async def _scan_redis_cache(self,
                                 keywords: list[str]) -> list[dict]:
        # Strip stop-words before scanning — prevents noise matches on
        # generic terms like "error", "fix", "update".
        meaningful_keywords = [
            k for k in keywords
            if k.lower() not in STOP_WORDS and len(k) > 2
        ]
        if not meaningful_keywords:
            log.warning("CrossSystem: all keywords were stop-words, "
                        "skipping redis scan")
            return []

        try:
            from ..redis_client import get_redis
            r    = await get_redis()
            keys = await r.keys("buglist:*")
            hits = []
            for key in keys:
                try:
                    val = await r.get(key)
                    if not val:
                        continue
                    bug_list = json.loads(val)
                    if not isinstance(bug_list, list):
                        continue
                    for bug in bug_list:
                        text = (
                            (bug.get("title") or "") + " " +
                            (bug.get("description") or "")
                        ).lower()
                        overlap = sum(
                            1 for kw in meaningful_keywords
                            if kw in text)
                        if overlap >= 1:
                            bug_id = (
                                bug.get("ticket_id") or
                                bug.get("id") or "")
                            system_type = (
                                bug.get("system_type") or
                                bug.get("source_id") or "unknown")
                            # Sanitize URL at read time (belt-and-suspenders)
                            clean_url = sanitize_bug_url(
                                url=bug.get("url", ""),
                                system_type=system_type,
                                bug_id=bug_id,
                            )
                            hits.append({
                                "id":            bug_id,
                                "ticket_id":     bug_id,
                                "title":         bug.get("title") or "",
                                "description":   (
                                    bug.get("description") or "")[:300],
                                "source":        system_type,
                                "system_type":   system_type,
                                "url":           clean_url,
                                "overlap_score": overlap,
                            })
                except Exception:
                    continue
            hits.sort(
                key=lambda x: x["overlap_score"], reverse=True)
            log.info("CrossSystem redis scan",
                     keys=len(keys), hits=len(hits))
            return hits[:20]
        except Exception as e:
            log.warning("CrossSystem redis scan failed",
                        error=str(e))
            return []

    # ── Groq batch scoring for cache candidates (Level 2) ────────
    async def _batch_score_with_groq(
            self,
            source_ticket: dict,
            candidates: list[dict]) -> list[dict]:
        MIN_RELEVANCE_SCORE = 0.5  # normalized 0.0-1.0

        def _normalize(raw: float) -> float:
            """Groq returns 0-10; frontend expects 0.0-1.0."""
            if isinstance(raw, (int, float)) and raw > 1.0:
                return round(raw / 10.0, 2)
            return round(float(raw), 2)

        def _label(score: float) -> str:
            if score >= 0.8:
                return "Very Similar"
            if score >= 0.6:
                return "Similar"
            return "Possible"

        def _alias_fields(c: dict) -> dict:
            """Map Level-2 field names to the Level-3 / TriagePage convention."""
            c.setdefault("ticket_id", c.get("id", ""))
            c.setdefault("system_type", c.get("source", ""))
            score = c.get("relevance_score", 0.0)
            c["similarity_score"]  = score
            c["similarity_reason"] = c.get("reason", "")
            c["similarity_label"]  = _label(score)
            return c

        groq_api_key = os.getenv("GROQ_API_KEY", "")
        if not groq_api_key:
            result = []
            for c in candidates:
                c["relevance_score"] = _normalize(
                    c.get("overlap_score", 0))
                if c["relevance_score"] >= MIN_RELEVANCE_SCORE:
                    result.append(_alias_fields(c))
            result.sort(
                key=lambda x: x.get("relevance_score", 0),
                reverse=True)
            if not result:
                log.info("All Level 2 candidates scored below "
                         "threshold (no-key path) — returning empty")
            return result[:10]

        cands_text = ""
        for i, c in enumerate(candidates):
            cands_text += (
                f"\n[{i}] ID: {c['id']}\n"
                f"Title: {c['title']}\n"
                f"Desc: {c['description'][:150]}\n"
            )

        prompt = (
            f"Score how relevant each candidate bug is to the "
            f"source bug.\n\n"
            f"Source bug:\n"
            f"Title: {source_ticket.get('title', '')}\n"
            f"Description: "
            f"{(source_ticket.get('description') or '')[:400]}\n\n"
            f"Candidate bugs:{cands_text}\n\n"
            f"Return ONLY a JSON array. Each element must have:\n"
            f'  {{"id": "...", "relevance_score": 0-10, '
            f'"reason": "one sentence"}}\n'
            f"No preamble, no explanation. JSON array only."
        )

        try:
            client = AsyncGroq(api_key=groq_api_key)
            resp   = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=800,
            )
            raw = (resp.choices[0].message.content or "[]")
            raw = raw.strip().strip("```json").strip(
                "```").strip()
            parsed = json.loads(raw)

            score_map = {}
            for item in (parsed
                         if isinstance(parsed, list) else []):
                try:
                    score_map[str(item["id"])] = {
                        "relevance_score": float(
                            item.get("relevance_score", 0)),
                        "reason": str(item.get("reason", "")),
                    }
                except Exception:
                    continue

            for c in candidates:
                sid = str(c["id"])
                if sid in score_map:
                    c["relevance_score"] = _normalize(
                        score_map[sid]["relevance_score"])
                    c["reason"] = score_map[sid]["reason"]
                else:
                    # Not scored by Groq — exclude rather than
                    # assign a phantom 0.5
                    c["relevance_score"] = 0.0
                    c["reason"] = ""

            # Normalize and threshold filter
            scored_candidates = [
                c for c in candidates
                if c.get("relevance_score", 0) >= MIN_RELEVANCE_SCORE
            ]
            scored_candidates.sort(
                key=lambda x: x.get("relevance_score", 0),
                reverse=True)

            if not scored_candidates:
                log.info("All Level 2 candidates scored below "
                         "threshold — returning empty list")
                return []

            return [_alias_fields(c)
                    for c in scored_candidates[:10]]

        except Exception as e:
            log.warning("CrossSystem Groq cache scoring failed",
                        error=str(e))
            result = []
            for c in candidates:
                c["relevance_score"] = round(
                    min(c.get("overlap_score", 0) / 10.0, 1.0), 2)
                if c["relevance_score"] >= MIN_RELEVANCE_SCORE:
                    result.append(_alias_fields(c))
            result.sort(
                key=lambda x: x.get("relevance_score", 0),
                reverse=True)
            return result[:10]

    # ── Live API search (Level 3 fallback) ───────────────────────
    async def _live_api_search(
            self,
            primary: dict,
            primary_source: str,
            context: dict,
            all_connectors: list | None = None) -> tuple[list, list]:
        groq_api_key = os.getenv("GROQ_API_KEY", "")
        groq_model   = os.getenv(
            "GROQ_MODEL", "llama-3.3-70b-versatile")

        # Step A: Generate platform-specific queries
        query_map = await self._generate_platform_queries(
            primary, groq_api_key, groq_model)
        log.info("CrossSystem queries",
                 queries=query_map,
                 source=primary_source)

        # Step B: Select target connectors (reuse pre-fetched list when available)
        if all_connectors is None:
            all_connectors = await ConnectorRegistry.get_all_enabled()
        targets = self._select_targets(
            all_connectors, primary_source)
        log.info("CrossSystem targets",
                 targets=[c.source_id for c in targets])

        if not targets:
            log.warning("CrossSystem: no targets found")
            return [], []

        # Step C: Multi-query parallel search
        # Each connector gets up to 3 diverse queries for better coverage:
        #   1. Specific (LLM-generated technical terms)
        #   2. Component + error type (direct from bug fields)
        #   3. Description keywords (extracted from description text)
        desc_query = self._extract_description_keywords(
            primary.get("description", ""),
            primary.get("title", ""))

        async def search_one(connector):
            ctype     = (connector.system_type or "").lower()
            source_id = connector.source_id

            # Build query list: all connectors get the specific query
            # (not just sisters), plus component+error and description
            queries = []

            # Query 1: Platform-specific technical query
            if "jira" in ctype:
                q = query_map.get("jira_query", "")
            elif "github" in ctype:
                q = query_map.get("github_query", "")
            elif "bugzilla" in ctype:
                q = query_map.get("bugzilla_query", "")
            else:
                q = query_map.get("broad_query", "")
            if q:
                queries.append(q)

            # Query 2: Component + error type (no LLM needed)
            comp_err = query_map.get("component_error_query", "")
            if comp_err and comp_err not in queries:
                queries.append(comp_err)

            # Query 3: Description keywords
            if desc_query and desc_query not in queries:
                queries.append(desc_query)

            if not queries:
                return source_id, []

            # Run each query and merge results (dedupe by ticket_id)
            all_results = []
            seen_tids = set()
            for query in queries[:3]:
                try:
                    results = await asyncio.wait_for(
                        connector.search(query, max_results=5),
                        timeout=20.0)
                    for r in results:
                        tid = getattr(r, "ticket_id", "") or ""
                        if tid not in seen_tids:
                            seen_tids.add(tid)
                            all_results.append(r)
                    log.info("CrossSystem search",
                             source=source_id,
                             query=query,
                             count=len(results))
                except asyncio.TimeoutError:
                    log.warning("CrossSystem timeout",
                                source=source_id, query=query)
                except Exception as e:
                    log.warning("CrossSystem error",
                                source=source_id,
                                query=query, error=str(e))

            log.info("CrossSystem multi-query results",
                     source=source_id,
                     queries_used=len(queries),
                     total_unique=len(all_results))
            return source_id, all_results

        gathered = await asyncio.gather(
            *[search_one(c) for c in targets])

        live_candidates = []
        sources_queried = []
        for source_id, tickets in gathered:
            sources_queried.append(source_id)
            for t in tickets:
                d = dataclasses.asdict(t)
                d["id"] = d.get("ticket_id", "")
                live_candidates.append(d)
            log.info("CrossSystem external source results",
                     source=source_id,
                     count=len(tickets))

        log.info("CrossSystem candidates",
                 total=len(live_candidates),
                 sources=sources_queried)

        # Step D: Add co-references as direct hits (score=1.0)
        direct_hits = await self._fetch_co_references(
            context, all_connectors, primary_source)
        for d in direct_hits:
            d["id"] = d.get("ticket_id", "")

        # Tier 2: if 0 results, try single-word fallback
        if not live_candidates and not direct_hits:
            primary_title = (primary.get("title") or "")
            fallback_map  = self._deterministic_fallback(
                primary_title,
                primary.get("component") or "")
            fallback_term = fallback_map.get("jira_query", "")

            if (fallback_term
                    and fallback_term != query_map.get(
                        "jira_query", "")):
                log.info("CrossSystem Tier2 fallback",
                         term=fallback_term)

                async def search_fallback(connector):
                    try:
                        r = await asyncio.wait_for(
                            connector.search(
                                fallback_term, max_results=8),
                            timeout=15.0)
                        return connector.source_id, r
                    except Exception:
                        return connector.source_id, []

                fb = await asyncio.gather(
                    *[search_fallback(c) for c in targets],
                    return_exceptions=True)
                for result in fb:
                    if isinstance(result, Exception):
                        continue
                    sid, tickets = result
                    for t in tickets:
                        d = dataclasses.asdict(t)
                        d["id"] = d.get("ticket_id", "")
                        live_candidates.append(d)

        log.info("CrossSystem candidates after fallback",
                 total=len(live_candidates))

        # Step E: Batch score ALL candidates (including co-references)
        # in ONE LLM call — co-refs should NOT get a free 1.0 score.
        all_to_score = live_candidates + direct_hits
        scored = await self._batch_score(
            primary, all_to_score, groq_api_key, groq_model)
        for d in scored:
            d["id"] = d.get("ticket_id", "")
            # Give a small relevance boost to co-references since
            # they were explicitly mentioned in the bug text
            if d.get("from_co_reference"):
                raw_score = d.get("similarity_score", 0)
                d["similarity_score"] = round(
                    min(1.0, raw_score + 0.1), 2)
                if d.get("similarity_reason"):
                    d["similarity_reason"] += (
                        " (explicitly referenced in bug text)")

        # Deduplicate
        seen_ids = set()
        final    = []
        for item in scored:
            tid = item.get("ticket_id", "") or item.get("id", "")
            if tid not in seen_ids:
                seen_ids.add(tid)
                final.append(item)

        return final, sources_queried

    # ── Query generation ──────────────────────────────────────────
    async def _generate_platform_queries(
            self, primary: dict,
            api_key: str, model: str) -> dict:
        title         = (primary.get("title") or "")
        component     = (primary.get("component") or "")
        error_excerpt = (primary.get("error_excerpt") or "")[:300]
        description   = (primary.get("description") or "")[:200]

        fallback = self._deterministic_fallback(title, component)

        if not api_key:
            return {**fallback,
                    "broad_query": component or
                    fallback["jira_query"]}

        prompt = f"""You are an expert open-source engineer.
Generate search terms to find duplicate bugs across
JIRA, GitHub, and Bugzilla.

Bug:
Title: {title}
Component: {component}
Error: {error_excerpt}
Description snippet: {description}

Rules:
- Use the exact technical vocabulary developers use in
  bug reports — NOT formal academic descriptions
- Apache projects use: "apache-rat-plugin", "rat check",
  "checkstyle", "findbugs", "spotbugs", "license header"
- Build/config issues use: file names like "eslintrc",
  "stylelintrc", "pom.xml", "build.gradle"
- Code issues use: class names, method names, exception types
- Strip ALL line numbers, hex addresses, thread IDs
- 2-4 words per query for better precision
- NEVER use: "management", "configuration", "implementation",
  "issue", "problem", "bug", "error", "fix", "missing"

Examples:
Title "Add license header to eslintrc.js"
  → specific: "eslintrc license header"
  → broad: "apache-rat license check"
  → component_error: "eslintrc rat"

Title "NullPointerException in StorageController.allocate"
  → specific: "StorageController allocate NPE"
  → broad: "storage allocation NullPointer"
  → component_error: "StorageController NullPointerException"

Title "Kafka consumer rebalancing timeout"
  → specific: "KafkaConsumer rebalancing timeout"
  → broad: "consumer group heartbeat rebalance"
  → component_error: "consumer rebalancing"

Output JSON only:
{{
  "jira_query":           "specific 2-4 developer terms",
  "github_query":         "specific 2-4 developer terms",
  "bugzilla_query":       "specific 2-4 developer terms",
  "broad_query":          "broad 2-4 ecosystem terms",
  "component_error_query": "component + error/symptom 2-3 words"
}}"""

        try:
            client = AsyncGroq(api_key=api_key)
            resp   = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=120,
            )
            raw    = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)

            generic = {"bug", "issue", "error", "fix",
                       "failure", "problem", "exception",
                       "crash", "null", ""}
            result  = {
                "jira_query":     str(parsed.get(
                    "jira_query", "")).strip()[:80],
                "github_query":   str(parsed.get(
                    "github_query", "")).strip()[:80],
                "bugzilla_query": str(parsed.get(
                    "bugzilla_query", "")).strip()[:80],
                "broad_query":    str(parsed.get(
                    "broad_query", "")).strip()[:80],
                "component_error_query": str(parsed.get(
                    "component_error_query", "")).strip()[:80],
            }

            for key in ["jira_query", "github_query",
                        "bugzilla_query"]:
                first = (result[key].split()[0].lower()
                         if result[key] else "")
                if not result[key] or first in generic:
                    result[key] = fallback.get(key, "")

            if not result["broad_query"]:
                result["broad_query"] = (component or
                                          fallback["jira_query"])
            # Deterministic component+error query as fallback
            if not result["component_error_query"] and component:
                result["component_error_query"] = component
            return result

        except Exception as e:
            log.warning("Query generation failed", error=str(e))
            fb = {**fallback,
                  "broad_query": component or
                  fallback["jira_query"]}
            if component:
                fb["component_error_query"] = component
            return fb

    def _deterministic_fallback(self,
                                 title: str,
                                 component: str) -> dict:
        import re

        # Words that are common in bug titles but useless
        # as search terms across systems
        STOP_WORDS = {
            "missing", "adding", "added", "update",
            "updated", "fix", "fixed", "fixing", "add",
            "remove", "removed", "change", "changed",
            "cannot", "failed", "unable", "invalid",
            "exception", "error", "failure", "issue",
            "problem", "wrong", "broken", "improve",
            "support", "should", "would", "could",
            "implement", "implementation", "using",
            "with", "from", "into", "this", "that",
        }

        # Prefer CamelCase identifiers (class/method names)
        camel = re.findall(
            r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', title)
        if camel:
            term = camel[0]
            return {
                "jira_query":     term[:50],
                "github_query":   term[:50],
                "bugzilla_query": term[:50],
            }

        # Prefer file extension patterns (.eslintrc, .stylelint)
        file_patterns = re.findall(
            r'\.[\w]+(?:rc|config|yml|yaml|json|js|ts)',
            title)
        if file_patterns:
            # Strip leading dot
            term = file_patterns[0][1:]
            return {
                "jira_query":     term[:50],
                "github_query":   term[:50],
                "bugzilla_query": term[:50],
            }

        # Prefer words > 5 chars that are NOT stop words
        # Skip words at position 0 (often verbs like Add/Fix)
        words = title.split()
        technical_words = [
            w for w in words[1:]  # skip first word
            if len(w) > 5
            and w.lower().strip(".,()[]") not in STOP_WORDS
            and not w.startswith(".")
        ]

        if technical_words:
            term = technical_words[0].strip(".,()[]")
        elif component:
            term = component
        elif words:
            # Last resort: any word > 4 chars not in stop words
            fallback_words = [
                w for w in words
                if len(w) > 4
                and w.lower() not in STOP_WORDS
            ]
            term = fallback_words[0] if fallback_words else words[-1]
        else:
            term = ""

        return {
            "jira_query":     term[:50],
            "github_query":   term[:50],
            "bugzilla_query": term[:50],
        }

    # ── Connector selection ───────────────────────────────────────
    def _get_family(self, source_id: str) -> str:
        """Derive a project 'family' name from a source_id by stripping
        known system-type suffixes. E.g. 'my-project-jira' and
        'my-project-github' both resolve to 'my-project'."""
        s = source_id.lower()
        for sx in ["-jira", "-github", "-bugzilla", "-gitlab",
                   "_jira", "_github", "_bugzilla", "_gitlab",
                   "-jira-cloud", "-jira-apache",
                   "_jira_cloud", "_jira_apache"]:
            if s.endswith(sx):
                s = s[:len(s) - len(sx)]
                break
        return s.strip("-_")

    def _select_targets(self,
                        all_connectors: list,
                        primary_source_id: str) -> list:
        """Select ALL external bug-source connectors for cross-system
        search. Connectors belonging to the same project family as
        the primary source are sorted first (sisters), followed by
        all other external systems."""
        candidates = [
            c for c in all_connectors
            if c.source_id != primary_source_id
            and getattr(c, "is_bug_source", False)
        ]
        if not candidates:
            return []

        pf = self._get_family(primary_source_id)
        # Sort by family affinity: sisters first, then others
        # Within each group, sort alphabetically for determinism
        candidates.sort(key=lambda c: (
            0 if self._get_family(c.source_id) == pf else 1,
            c.source_id,
        ))
        log.info("CrossSystem target selection",
                 primary_family=pf,
                 total_external=len(candidates),
                 targets=[c.source_id for c in candidates[:12]])
        return candidates[:12]

    # ── Backlink extraction (pure text, no network) ───────────────
    def _extract_backlinks(self,
                            description: str,
                            comments: list[str]) -> dict:
        import re
        combined = " ".join([description] + list(comments))

        jira_ids     = set(re.findall(
            r'\b([A-Z][A-Z0-9]+-\d+)\b', combined))
        github_refs  = set(re.findall(
            r'#(\d+)', combined))
        bugzilla_ids = set(re.findall(
            r'\bBZ-(\d+)\b', combined))

        return {
            "jira":     sorted(jira_ids),
            "github":   sorted(github_refs),
            "bugzilla": sorted(bugzilla_ids),
        }

    # ── Co-reference direct hits ──────────────────────────────────
    async def _fetch_co_references(
            self, context: dict,
            all_connectors: list,
            primary_source: str) -> list:
        co_refs  = context.get("co_references") or []
        hits     = []

        for ref in co_refs[:5]:
            for c in all_connectors:
                if (c.source_id == primary_source
                        or not getattr(c, "is_bug_source", False)):
                    continue
                try:
                    t = await asyncio.wait_for(
                        c.get_ticket(ref["raw_id"]),
                        timeout=8.0)
                    if t:
                        td = dataclasses.asdict(t)
                        # Mark as co-reference for scoring boost,
                        # but do NOT auto-assign 1.0 — let AI
                        # scoring validate actual relevance.
                        td["from_co_reference"] = True
                        td["similarity_score"]    = 0.6
                        td["similarity_label"]    = "Referenced"
                        td["similarity_reason"]   = (
                            "Cross-reference found in bug text")
                        td["similarity_matching_fields"] = [
                            "direct_reference"]
                        hits.append(td)
                        log.info("CrossSystem co-ref found",
                                 ref=ref["raw_id"],
                                 source=c.source_id,
                                 title=(td.get("title") or "")[:60])
                        break
                except Exception:
                    pass
        return hits

    # ── Batch scoring (ONE LLM call) ─────────────────────────────
    async def _batch_score(self,
                            primary: dict,
                            candidates: list,
                            api_key: str,
                            model: str) -> list:
        if not candidates:
            return []

        if not api_key:
            for c in candidates:
                c["similarity_score"]          = 0.4
                c["similarity_label"]          = "Possible"
                c["similarity_reason"]         = "No AI scoring"
                c["similarity_matching_fields"] = []
            return candidates

        primary_str = (
            f"Title: {primary.get('title', '')}\n"
            f"Component: {primary.get('component', '')}\n"
            f"Severity: {primary.get('severity', '')}\n"
            f"Error: {(primary.get('error_excerpt') or '')[:300]}\n"
            f"Description: {(primary.get('description') or '')[:300]}"
        )

        cands_str = ""
        for i, c in enumerate(candidates[:12]):
            cands_str += (
                f"\n[{i}] id={c.get('ticket_id')} "
                f"source={c.get('source_id')}\n"
                f"Title: {c.get('title', '')}\n"
                f"Component: {c.get('component', '')}\n"
                f"Description: "
                f"{(c.get('description') or '')[:150]}\n"
            )

        prompt = (
            f"Score how related each candidate is to the "
            f"primary bug. Be STRICT — only bugs with the same "
            f"root cause or same error in the same code area "
            f"should score high.\n\n"
            f"Primary:\n{primary_str}\n\n"
            f"Candidates:{cands_str}\n\n"
            f"Return JSON object with key 'results' as array.\n"
            f"Each item: index, ticket_id, "
            f"similarity_score (0.0-1.0),\n"
            f"similarity_label "
            f"(Identical/Very Similar/Similar/Possible/Unrelated),\n"
            f"similarity_reason (one sentence), "
            f"similarity_matching_fields (array).\n\n"
            f"Strict scoring calibration:\n"
            f"0.9+ = same root cause AND same error/exception "
            f"in same code path\n"
            f"0.7-0.89 = same error type in the same component, "
            f"likely same root cause\n"
            f"0.5-0.69 = related symptoms or same failure mode "
            f"but may differ in root cause\n"
            f"0.3-0.49 = same area/component but clearly "
            f"different issue\n"
            f"0.0-0.29 = unrelated, different component or "
            f"different type of issue entirely\n\n"
            f"IMPORTANT: Two bugs sharing ONLY a component name "
            f"but with different errors/symptoms MUST score "
            f"below 0.5. Sharing a component is NOT enough.\n"
            f"Return JSON only."
        )

        try:
            client = AsyncGroq(api_key=api_key)
            resp   = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                response_format={"type": "json_object"},
                max_tokens=1500,
            )
            raw    = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)

            scores_list = (
                parsed.get("results")
                or parsed.get("scores")
                or parsed.get("candidates")
                or (parsed if isinstance(parsed, list) else [])
            )

            score_map = {}
            for s in scores_list:
                try:
                    v = CandidateScore(**s)
                    score_map[str(v.ticket_id)] = v
                except Exception:
                    continue

            for c in candidates[:12]:
                tid = str(c.get("ticket_id", ""))
                if tid in score_map:
                    v = score_map[tid]
                    c["similarity_score"]    = v.similarity_score
                    c["similarity_label"]    = v.similarity_label
                    c["similarity_reason"]   = v.similarity_reason
                    c["similarity_matching_fields"] = (
                        v.similarity_matching_fields)
                else:
                    c["similarity_score"]    = 0.25
                    c["similarity_label"]    = "Possible"
                    c["similarity_reason"]   = "Not scored"
                    c["similarity_matching_fields"] = []

        except Exception as e:
            log.warning("Batch scoring failed", error=str(e))
            for c in candidates:
                c["similarity_score"]          = 0.25
                c["similarity_label"]          = "Possible"
                c["similarity_reason"]         = "Unavailable"
                c["similarity_matching_fields"] = []

        result = [c for c in candidates[:12]
                  if c.get("similarity_score", 0) >= 0.55]
        result.sort(
            key=lambda x: x.get("similarity_score", 0),
            reverse=True)
        return result

    # ── Description keyword extraction ──────────────────────────
    def _extract_description_keywords(
            self, description: str, title: str) -> str:
        """Extract 3-4 meaningful technical keywords from the
        description to use as a complementary search query.
        Prioritizes exception types, class names, file paths,
        and technical terms."""
        import re
        text = f"{title} {description[:500]}"

        # Priority 1: Exception/Error class names
        exceptions = re.findall(
            r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)*(?:Exception|Error|'
            r'Fault|Failure))\b', text)

        # Priority 2: CamelCase identifiers (class/method names)
        camel = re.findall(
            r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text)
        # Remove duplicates with exceptions
        camel = [c for c in camel if c not in exceptions]

        # Priority 3: File paths / config names
        files = re.findall(
            r'\b([\w.-]+\.(?:java|py|js|ts|xml|yml|yaml|conf|'
            r'properties|json|gradle|scala))\b', text)

        # Priority 4: Long technical words (>6 chars, not stop words)
        tech_words = [
            w.strip(".,()[]'\"") for w in text.split()
            if len(w.strip(".,()[]'\"")) > 6
            and w.lower().strip(".,()[]'\"") not in STOP_WORDS
            and not w[0].isdigit()
        ]

        # Combine: take best from each category
        keywords = []
        seen = set()
        for pool in [exceptions[:2], camel[:2],
                     files[:1], tech_words[:3]]:
            for kw in pool:
                kw_lower = kw.lower()
                if kw_lower not in seen and len(kw) > 2:
                    seen.add(kw_lower)
                    keywords.append(kw)
                if len(keywords) >= 4:
                    break
            if len(keywords) >= 4:
                break

        result = " ".join(keywords[:4])
        log.info("CrossSystem description keywords",
                 keywords=result)
        return result
