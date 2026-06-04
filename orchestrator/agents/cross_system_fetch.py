import asyncio
import dataclasses
import os
import json
import structlog
from groq import AsyncGroq
from .base import BaseAgent
from ..connectors.registry import ConnectorRegistry
from ..models.synthesis import CandidateScore

log = structlog.get_logger()


class CrossSystemFetchAgent(BaseAgent):
    step_name = "cross_system_fetch"

    async def run(self, context: dict) -> dict:
        primary        = context.get("primary_ticket") or {}
        primary_source = context.get("source_id", "")

        if not primary:
            log.warning("CrossSystem: no primary ticket")
            context["related_tickets"]    = []
            context["sources_queried"]    = []
            context["related_candidates"] = []
            return context

        if not primary.get("title"):
            log.warning("CrossSystem: primary ticket empty")
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

        # LEVEL 1: Backlink extraction
        backlinks  = self._extract_backlinks(description, comments)
        candidates = []
        for jira_id in backlinks["jira"]:
            candidates.append({
                "id":            jira_id,
                "source":        "jira",
                "from_backlink": True,
                "title":         jira_id,
                "description":   "",
                "overlap_score": 999,
            })
        for gh_id in backlinks["github"]:
            candidates.append({
                "id":            f"#{gh_id}",
                "source":        "github",
                "from_backlink": True,
                "title":         f"GitHub #{gh_id}",
                "description":   "",
                "overlap_score": 999,
            })
        for bz_id in backlinks["bugzilla"]:
            candidates.append({
                "id":            f"BZ-{bz_id}",
                "source":        "bugzilla",
                "from_backlink": True,
                "title":         f"BZ-{bz_id}",
                "description":   "",
                "overlap_score": 999,
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

        # LEVEL 3: Live API search — only if < 2 unique candidates
        total_unique = len({c["id"] for c in candidates})
        if total_unique < 2:
            log.warning(
                "Levels 1+2 returned fewer than 2 candidates. "
                "Falling back to live API search.")
            live_results, sources_queried = await self._live_api_search(
                primary, primary_source, context)
            candidates.extend(live_results)
            context["related_tickets"]  = live_results
            context["sources_queried"]  = sources_queried
        else:
            context["related_tickets"]  = candidates
            context["sources_queried"]  = []

        context["related_candidates"] = candidates
        return context

    # ── Redis cache scan (Level 2) ────────────────────────────────
    async def _scan_redis_cache(self,
                                 keywords: list[str]) -> list[dict]:
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
                        text    = (
                            (bug.get("title") or "") + " " +
                            (bug.get("description") or "")
                        ).lower()
                        overlap = sum(
                            1 for kw in keywords if kw in text)
                        if overlap >= 1:
                            hits.append({
                                "id":           (
                                    bug.get("ticket_id") or
                                    bug.get("id") or ""),
                                "title":        bug.get("title") or "",
                                "description":  (
                                    bug.get("description") or "")[:300],
                                "source":       (
                                    bug.get("system_type") or
                                    bug.get("source_id") or "unknown"),
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
        groq_api_key = os.getenv("GROQ_API_KEY", "")
        if not groq_api_key:
            for c in candidates:
                c["relevance_score"] = 5.0
            return sorted(
                candidates,
                key=lambda x: x.get("overlap_score", 0),
                reverse=True)[:10]

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
                            item.get("relevance_score", 5)),
                        "reason": str(item.get("reason", "")),
                    }
                except Exception:
                    continue

            for c in candidates:
                sid = str(c["id"])
                if sid in score_map:
                    c["relevance_score"] = (
                        score_map[sid]["relevance_score"])
                    c["reason"] = score_map[sid]["reason"]
                else:
                    c["relevance_score"] = 5.0

            candidates.sort(
                key=lambda x: x.get("relevance_score", 0),
                reverse=True)
            return candidates[:10]

        except Exception as e:
            log.warning("CrossSystem Groq cache scoring failed",
                        error=str(e))
            for c in candidates:
                c["relevance_score"] = float(
                    c.get("overlap_score", 5))
            candidates.sort(
                key=lambda x: x.get("relevance_score", 0),
                reverse=True)
            return candidates[:10]

    # ── Live API search (Level 3 fallback) ───────────────────────
    async def _live_api_search(
            self,
            primary: dict,
            primary_source: str,
            context: dict) -> tuple[list, list]:
        groq_api_key = os.getenv("GROQ_API_KEY", "")
        groq_model   = os.getenv(
            "GROQ_MODEL", "llama-3.3-70b-versatile")

        # Step A: Generate platform-specific queries
        query_map = await self._generate_platform_queries(
            primary, groq_api_key, groq_model)
        log.info("CrossSystem queries",
                 queries=query_map,
                 source=primary_source)

        # Step B: Select target connectors
        all_connectors = await ConnectorRegistry.get_all_enabled()
        targets = self._select_targets(
            all_connectors, primary_source)
        log.info("CrossSystem targets",
                 targets=[c.source_id for c in targets])

        if not targets:
            log.warning("CrossSystem: no targets found")
            return [], []

        # Step C: Parallel search with platform-specific queries
        async def search_one(connector):
            ctype     = type(connector).__name__.lower()
            source_id = connector.source_id
            is_sister = (self._get_family(source_id) ==
                         self._get_family(primary_source))

            if "jira" in ctype:
                query = (query_map.get("jira_query", "")
                         if is_sister
                         else query_map.get("broad_query", ""))
            elif "github" in ctype:
                query = (query_map.get("github_query", "")
                         if is_sister
                         else query_map.get("broad_query", ""))
            elif "bugzilla" in ctype:
                query = (query_map.get("bugzilla_query", "")
                         if is_sister
                         else query_map.get("broad_query", ""))
            else:
                query = query_map.get("broad_query", "")

            if not query:
                return source_id, []

            try:
                results = await asyncio.wait_for(
                    connector.search(query, max_results=8),
                    timeout=20.0)
                log.info("CrossSystem result",
                         source=source_id,
                         query=query,
                         sister=is_sister,
                         count=len(results))
                return source_id, results
            except asyncio.TimeoutError:
                log.warning("CrossSystem timeout",
                            source=source_id)
                return source_id, []
            except Exception as e:
                log.warning("CrossSystem error",
                            source=source_id, error=str(e))
                return source_id, []

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

        log.info("CrossSystem candidates",
                 total=len(live_candidates))

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

        # Step E: Batch score all candidates in ONE LLM call
        scored = await self._batch_score(
            primary, live_candidates, groq_api_key, groq_model)
        for d in scored:
            d["id"] = d.get("ticket_id", "")

        # Merge direct hits first then scored
        seen_ids = set()
        final    = []
        for item in direct_hits + scored:
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

Rules:
- Use the exact technical vocabulary developers use in
  bug reports — NOT formal academic descriptions
- Apache projects use: "apache-rat-plugin", "rat check",
  "checkstyle", "findbugs", "spotbugs", "license header"
- Build/config issues use: file names like "eslintrc",
  "stylelintrc", "pom.xml", "build.gradle"
- Code issues use: class names, method names, exception types
- Strip ALL line numbers, hex addresses, thread IDs
- 1-2 words maximum per query
- NEVER use: "management", "configuration", "implementation",
  "issue", "problem", "bug", "error", "fix", "missing"

Examples:
Title "Add license header to eslintrc.js"
  → specific: "eslintrc license"
  → broad: "apache-rat license"

Title "NullPointerException in StorageController.allocate"
  → specific: "StorageController NPE"
  → broad: "storage allocation concurrent"

Title "Kafka consumer rebalancing timeout"
  → specific: "KafkaConsumer rebalancing"
  → broad: "consumer group heartbeat"

Output JSON only:
{{
  "jira_query":     "specific 1-2 developer terms",
  "github_query":   "specific 1-2 developer terms",
  "bugzilla_query": "specific 1-2 developer terms",
  "broad_query":    "broad 1-2 ecosystem terms"
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
                    "jira_query", "")).strip()[:60],
                "github_query":   str(parsed.get(
                    "github_query", "")).strip()[:60],
                "bugzilla_query": str(parsed.get(
                    "bugzilla_query", "")).strip()[:60],
                "broad_query":    str(parsed.get(
                    "broad_query", "")).strip()[:60],
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
            return result

        except Exception as e:
            log.warning("Query generation failed", error=str(e))
            return {**fallback,
                    "broad_query": component or
                    fallback["jira_query"]}

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
        s = source_id.lower()
        for p in ["apache-", "mozilla-", "microsoft-",
                  "kubernetes-", "facebook-", "nodejs-"]:
            s = s.replace(p, "")
        for sx in ["-jira", "-github", "-bugzilla",
                   "-gitlab"]:
            s = s.replace(sx, "")
        return s.strip("-")

    def _select_targets(self,
                        all_connectors: list,
                        primary_source_id: str) -> list:
        excluded = {"confluence", "customer_portal"}
        candidates = [
            c for c in all_connectors
            if c.source_id != primary_source_id
            and c.system_type not in excluded
        ]
        if not candidates:
            return []

        pf     = self._get_family(primary_source_id)
        apache = {"spark", "kafka", "hadoop", "hive",
                  "flink", "hbase", "cassandra",
                  "airflow", "zookeeper"}

        sisters = [c for c in candidates
                   if self._get_family(c.source_id) == pf]
        related = [c for c in candidates
                   if self._get_family(c.source_id) in apache
                   and self._get_family(c.source_id) != pf
                   and c not in sisters]

        seen   = {c.system_type for c in sisters + related}
        others = []
        for c in candidates:
            if (c not in sisters and c not in related
                    and c.system_type not in seen):
                others.append(c)
                seen.add(c.system_type)

        return (sisters + related[:3] + others[:2])[:6]

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
        excluded = {"confluence", "customer_portal"}

        for ref in co_refs[:5]:
            for c in all_connectors:
                if (c.source_id == primary_source
                        or c.system_type in excluded):
                    continue
                try:
                    t = await asyncio.wait_for(
                        c.get(ref["raw_id"]),
                        timeout=8.0)
                    if t:
                        td = dataclasses.asdict(t)
                        td["similarity_score"]    = 1.0
                        td["similarity_label"]    = "Identical"
                        td["similarity_reason"]   = (
                            "Explicit cross-reference "
                            "in bug text")
                        td["similarity_matching_fields"] = [
                            "direct_reference"]
                        hits.append(td)
                        log.info("CrossSystem direct hit",
                                 ref=ref["raw_id"],
                                 source=c.source_id)
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
            f"primary bug.\n"
            f"Focus on: same root cause, same component, "
            f"same exception, same code path.\n\n"
            f"Primary:\n{primary_str}\n\n"
            f"Candidates:{cands_str}\n\n"
            f"Return JSON object with key 'results' as array.\n"
            f"Each item: index, ticket_id, "
            f"similarity_score (0.0-1.0),\n"
            f"similarity_label "
            f"(Identical/Very Similar/Similar/Possible/Unrelated),\n"
            f"similarity_reason (one sentence), "
            f"similarity_matching_fields (array).\n"
            f"0.9+ same root cause, 0.7 same component+error, "
            f"0.5 same component, 0.3 related, 0.1 unrelated.\n"
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
                  if c.get("similarity_score", 0) >= 0.50]
        result.sort(
            key=lambda x: x.get("similarity_score", 0),
            reverse=True)
        return result
