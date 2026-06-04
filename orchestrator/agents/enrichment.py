import asyncio
import json
import math
import os
import re
import time
import structlog
import httpx
from groq import AsyncGroq
from .base import BaseAgent
from ..connectors.registry import ConnectorRegistry

log = structlog.get_logger()

MAX_REACT_ITERS = 4

# Map source_id family to Apache Confluence space keys
SOURCE_TO_SPACE = {
    "spark":      "SPARK",
    "kafka":      "KAFKA",
    "flink":      "FLINK",
    "hadoop":     "HADOOP",
    "hive":       "HIVE",
    "hbase":      "HBASE",
    "zookeeper":  "ZOOKEEPER",
    "cassandra":  "CASSANDRA",
    "airflow":    "AIRFLOW",
}

SYSTEM_PROMPT = """You are a technical documentation \
specialist in a strict ReAct loop.
Find the most relevant troubleshooting articles for the bug.

Tools:
Action: search_confluence
Action Input: <2-4 word query>

Rules:
- Use DEVELOPER vocabulary not formal descriptions
- Apache projects: use "apache-rat", "checkstyle",
  "rat plugin", "license header", "eslintrc"
- JVM issues: use class name + exception type
- Config issues: use exact filename
- If search returns nothing, try a different angle
- Maximum 4 searches

Format:
Thought: <reasoning>
Action: search_confluence
Action Input: <query>

OR:
Final Answer: [{"title":"...","url":"...",
"excerpt":"...","relevance":"high|medium|low"}]

Always provide Final Answer even if empty."""


class EnrichmentAgent(BaseAgent):
    step_name = "enrichment"

    def _get_family(self, source_id: str) -> str:
        s = source_id.lower()
        for p in ["apache-", "mozilla-", "microsoft-",
                  "kubernetes-"]:
            s = s.replace(p, "")
        for sx in ["-jira", "-github", "-bugzilla"]:
            s = s.replace(sx, "")
        return s.strip("-")

    def _get_target_space(self, source_id: str) -> str:
        family = self._get_family(source_id)
        return SOURCE_TO_SPACE.get(family, "HPEKB")

    def _extract_initial_query(self, primary: dict) -> str:
        title     = (primary.get("title") or "")
        component = (primary.get("component") or "")
        error     = (primary.get("error_excerpt") or "")[:200]

        # File names: .eslintrc, .stylelintrc, pom.xml
        files = re.findall(
            r'\.[\w]+(?:rc|config|yml|yaml|json|js|ts)',
            title)
        if files:
            return files[0][1:]

        # CamelCase class names
        camel = re.findall(
            r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b',
            title + " " + error)
        if camel:
            return camel[0]

        # Exception types in error
        exc = re.findall(
            r'\b\w+(?:Exception|Error)\b', error)
        if exc:
            return exc[0]

        # Component + first meaningful title word
        stop = {"add", "fix", "update", "remove",
                "missing", "error", "failed"}
        words = [
            w.strip(".,()") for w in title.split()
            if len(w) > 4
            and w.lower() not in stop
        ]
        if words and component:
            return f"{component} {words[0]}"
        if words:
            return words[0]

        return component or title[:30]

    async def run(self, context: dict) -> dict:
        primary   = context.get("primary_ticket") or {}
        source_id = context.get("source_id", "")

        title         = (primary.get("title") or "")
        component     = (primary.get("component") or "")
        description   = (primary.get(
            "description") or "")[:400]
        error_excerpt = (primary.get(
            "error_excerpt") or "")[:300]

        groq_api_key    = os.getenv("GROQ_API_KEY", "")
        enrichment_model = "llama-3.1-8b-instant"

        # Determine correct Confluence space
        target_space = self._get_target_space(source_id)
        log.info("Enrichment target space",
                 source_id=source_id,
                 space=target_space)

        # Extract deterministic initial query
        initial_query = self._extract_initial_query(primary)
        log.info("Enrichment initial query",
                 query=initial_query)

        kb_articles  = []
        so_articles  = []

        # Run Confluence ReAct + Stack Overflow in parallel
        confluence_task = asyncio.create_task(
            self._run_react_loop(
                title, component, description,
                error_excerpt, initial_query,
                target_space, groq_api_key,
                enrichment_model))

        so_task = asyncio.create_task(
            self._search_stackoverflow(
                initial_query, title))

        conf_result, so_result = await asyncio.gather(
            confluence_task, so_task,
            return_exceptions=True)

        if isinstance(conf_result, list):
            kb_articles = conf_result
        if isinstance(so_result, list):
            so_articles = so_result

        # Merge: confluence first then Stack Overflow
        all_articles = kb_articles + so_articles
        log.info("Enrichment complete",
                 confluence=len(kb_articles),
                 stackoverflow=len(so_articles),
                 total=len(all_articles))

        context["kb_articles"] = all_articles[:6]
        return context

    async def _run_react_loop(
            self, title: str, component: str,
            description: str, error_excerpt: str,
            initial_query: str, target_space: str,
            api_key: str, model: str) -> list:
        if not api_key:
            return []

        client   = AsyncGroq(api_key=api_key)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Find articles for this bug:\n"
                    f"Title: {title}\n"
                    f"Component: {component}\n"
                    f"Description: {description}\n"
                    f"Error: {error_excerpt}\n\n"
                    f"Start with this search query: "
                    f"{initial_query}"
                ),
            },
        ]

        for iteration in range(MAX_REACT_ITERS):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=512,
                )
                reply = (
                    resp.choices[0].message.content or "")
                messages.append({
                    "role": "assistant",
                    "content": reply})

                if "Final Answer:" in reply:
                    raw = reply.split(
                        "Final Answer:")[-1].strip()
                    raw = raw.strip(
                        "```json").strip("```").strip()
                    try:
                        parsed = json.loads(raw)
                        return (parsed
                                if isinstance(parsed, list)
                                else [])
                    except Exception:
                        return []

                if ("Action: search_confluence" in reply
                        and "Action Input:" in reply):
                    query = (
                        reply.split("Action Input:")[-1]
                        .strip()
                        .split("\n")[0]
                        .strip()
                        .strip('"\''))

                    results = await self._search_confluence(
                        query, target_space)

                    if not results:
                        obs = (
                            f"No results for '{query}' in "
                            f"{target_space} space. Try a "
                            f"different technical term or "
                            f"broader concept.")
                    else:
                        obs = json.dumps(results)

                    messages.append({
                        "role": "user",
                        "content": f"Observation: {obs}",
                    })

            except Exception as e:
                log.warning("ReAct iteration failed",
                            error=str(e),
                            iteration=iteration)
                break

        return []

    async def _search_confluence(
            self, query: str,
            target_space: str) -> list[dict]:
        try:
            connectors = await ConnectorRegistry.get_all_by_type(
                "confluence")
            if not connectors:
                try:
                    all_c = await ConnectorRegistry.get_all_enabled()
                    connectors = [
                        c for c in all_c
                        if c.system_type == "confluence"
                    ]
                except Exception:
                    return []

            if not connectors:
                return []

            # Find connector matching target space
            # Fall back to first confluence connector
            target_connector = None
            for c in connectors:
                if (c.project_key or "").upper() == (
                        target_space.upper()):
                    target_connector = c
                    break
            if not target_connector:
                target_connector = connectors[0]

            results = await asyncio.wait_for(
                target_connector.search(
                    query, max_results=5),
                timeout=15.0)

            output = []
            for t in results:
                article_text = t.description or ""
                chunks       = self._slice_and_score(
                    article_text, query, 0)
                excerpt      = " ... ".join(chunks)[:400]
                output.append({
                    "title":     t.title,
                    "url":       t.url,
                    "excerpt":   excerpt,
                    "relevance": "medium",
                    "source":    "confluence",
                })
            log.info("Confluence search",
                     query=query,
                     space=target_space,
                     count=len(output))
            return output

        except asyncio.TimeoutError:
            log.warning("Confluence timeout",
                        query=query)
            return []
        except Exception as e:
            log.warning("Confluence error",
                        error=str(e))
            return []

    async def _search_stackoverflow(
            self, query: str,
            title: str) -> list[dict]:
        try:
            search_q = query or title[:50]
            url      = "https://api.stackexchange.com/2.3/search"
            params   = {
                "order":    "desc",
                "sort":     "relevance",
                "intitle":  search_q,
                "site":     "stackoverflow",
                "pagesize": 5,
            }
            async with httpx.AsyncClient(
                    timeout=10,
                    follow_redirects=True) as client:
                resp = await client.get(
                    url, params=params)
                if resp.status_code != 200:
                    return []
                items   = resp.json().get("items", [])
                results = []
                for item in items:
                    score     = item.get("score", 0)
                    answered  = item.get("is_answered",
                                         False)
                    relevance = (
                        "high"
                        if answered and score > 5
                        else "medium"
                        if answered
                        else "low")
                    results.append({
                        "title":     item.get("title", ""),
                        "url":       (
                            "https://stackoverflow.com"
                            f"/questions/"
                            f"{item.get('question_id')}"),
                        "excerpt":   (
                            f"Score: {score} | "
                            f"Answered: {answered} | "
                            f"Tags: "
                            f"{', '.join(item.get('tags', [])[:4])}"),
                        "relevance": relevance,
                        "source":    "stackoverflow",
                    })
                log.info("StackOverflow search",
                         query=search_q,
                         count=len(results))
                return results
        except Exception as e:
            log.warning("StackOverflow error",
                        error=str(e))
            return []

    def _slice_and_score(self,
                          article_text: str,
                          bug_text: str,
                          last_modified_epoch: float = 0
                          ) -> list[str]:
        paragraphs = [
            p.strip()
            for p in article_text.split("\n\n")
            if len(p.strip()) > 30
        ]
        if not paragraphs:
            return [article_text[:500]]

        if last_modified_epoch and last_modified_epoch > 0:
            delta = ((time.time() - last_modified_epoch)
                     / (365 * 24 * 3600))
            decay = math.exp(-0.15 * delta)
        else:
            decay = 0.9

        bug_words = set(bug_text.lower().split())
        scored    = []
        for chunk in paragraphs:
            cwords  = set(chunk.lower().split())
            if not cwords:
                continue
            overlap  = (len(bug_words & cwords)
                        / len(bug_words | cwords))
            adjusted = overlap * decay
            has_fix  = any(
                kw in chunk.lower()
                for kw in ("workaround", "patch", "fix",
                           "resolution", "solution"))
            if adjusted >= 0.08 or has_fix:
                scored.append((adjusted, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [c for _, c in scored[:3]]
        return top if top else [article_text[:500]]
