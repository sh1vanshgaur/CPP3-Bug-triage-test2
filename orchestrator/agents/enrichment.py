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

    SOURCE_SPACE_MAP = {
        "apache-flink":     "FLINK",
        "flink":            "FLINK",
        "apache-spark":     "SPARK",
        "spark":            "SPARK",
        "kafka":            "KAFKA",
        "apache-kafka":     "KAFKA",
        "hpe":              "HPEKB",
        "hpekb":            "HPEKB",
        "hadoop":           "HADOOP",
        "apache-hadoop":    "HADOOP",
        "zookeeper":        "ZOOKEEPER",
        "apache-zookeeper": "ZOOKEEPER",
    }
    DEFAULT_SPACE = "HPEKB"

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

    def _resolve_target_space(self, source_id: str) -> str:
        s = source_id.lower().rstrip("0123456789-")
        for key, space in self.SOURCE_SPACE_MAP.items():
            if s.startswith(key) or key in s:
                return space
        return self.DEFAULT_SPACE

    def _extract_initial_query(self,
                               ticket_title: str,
                               ticket_description: str) -> str:
        # Rule 1: file extension pattern
        files = re.findall(
            r'\.[\w]+(?:rc|config|yml|yaml|json|js|ts|xml|toml)',
            ticket_title)
        if files:
            return files[0][1:]

        # Rule 2: CamelCase word
        camel = re.findall(
            r'\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b', ticket_title)
        if camel:
            return camel[0]

        # Rule 3: Exception/Error/Failure suffix
        exc = re.findall(
            r'\b\w+(?:Exception|Error|Failure)\b', ticket_title)
        if exc:
            return exc[0]

        # Rule 4: first 4 words of title
        words = [
            w.strip(".,()[]\"'") for w in ticket_title.split()]
        return " ".join(words[:4])

    GITHUB_REPO_MAP = {
        "FLINK":     "apache/flink",
        "SPARK":     "apache/spark",
        "KAFKA":     "apache/kafka",
        "HADOOP":    "apache/hadoop",
        "ZOOKEEPER": "apache/zookeeper",
        "HPEKB":     "HewlettPackard/hpe-dev-portal",
    }

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
        target_space = self._resolve_target_space(source_id)
        log.info("Enrichment target space",
                 source_id=source_id,
                 space=target_space)

        # Extract deterministic initial query
        initial_query = self._extract_initial_query(
            title, description)
        log.info("Enrichment initial query",
                 query=initial_query)

        kb_articles = []

        # ReAct loop: iteration 0 runs Confluence + SO in
        # parallel internally; iterations 1+ refine via LLM
        self._iter0_so_results = []
        conf_result = await self._run_react_loop(
            title, component, description,
            error_excerpt, initial_query,
            target_space, groq_api_key,
            enrichment_model)

        if isinstance(conf_result, list):
            kb_articles = conf_result

        so_articles = self._iter0_so_results

        # BUG5: run Apache JIRA and GitHub Issues in parallel with
        # the existing sources for richer enrichment
        github_repo  = self.GITHUB_REPO_MAP.get(target_space, "")
        apache_task  = (
            self._fetch_apache_jira(initial_query, target_space)
            if target_space and target_space != "HPEKB"
            else asyncio.sleep(0, result=[])
        )
        github_task  = (
            self._fetch_github_issues(initial_query, github_repo)
            if github_repo
            else asyncio.sleep(0, result=[])
        )
        apache_results, gh_results = await asyncio.gather(
            apache_task, github_task,
            return_exceptions=True,
        )
        apache_articles = apache_results if isinstance(apache_results, list) else []
        gh_articles     = gh_results     if isinstance(gh_results,     list) else []
        if isinstance(apache_results, Exception):
            log.warning("Apache JIRA fetch failed",
                        error=str(apache_results))
        if isinstance(gh_results, Exception):
            log.warning("GitHub Issues fetch failed",
                        error=str(gh_results))

        # Tag every item with its source
        for item in kb_articles:
            item.setdefault("source", "confluence")
        for item in so_articles:
            item.setdefault("source", "stackoverflow")
        for item in apache_articles:
            item.setdefault("source", "apache_jira")
        for item in gh_articles:
            item.setdefault("source", "github")

        # Merge: confluence first, then SO, then Apache JIRA, then GitHub
        all_articles = kb_articles + so_articles + apache_articles + gh_articles
        log.info("Enrichment complete",
                 confluence=len(kb_articles),
                 stackoverflow=len(so_articles),
                 apache_jira=len(apache_articles),
                 github=len(gh_articles),
                 total=len(all_articles))

        context["kb_articles"]        = all_articles[:6]
        context["enrichment_sources"] = all_articles
        return context

    async def _fetch_apache_jira(self,
                                  query: str,
                                  project_key: str) -> list[dict]:
        """Search Apache's public JIRA for related issues (no auth needed)."""
        url = "https://issues.apache.org/jira/rest/api/2/search"
        params = {
            "jql": (
                f'project = {project_key} AND text ~ "{query}" '
                f"ORDER BY updated DESC"
            ),
            "maxResults": 5,
            "fields": "summary,status,description,comment,assignee,priority",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                issues = resp.json().get("issues", [])
                results = []
                for i in issues:
                    results.append({
                        "id":          i["key"],
                        "title":       i["fields"]["summary"],
                        "url":         f"https://issues.apache.org/jira/browse/{i['key']}",
                        "status":      i["fields"]["status"]["name"],
                        "source":      "apache_jira",
                        "description": (i["fields"].get("description") or "")[:300],
                        "excerpt":     (i["fields"].get("description") or "")[:200],
                        "relevance":   "medium",
                    })
                log.info("Apache JIRA search",
                         project=project_key, query=query,
                         count=len(results))
                return results
        except Exception as e:
            log.warning("Apache JIRA search failed",
                        project=project_key, error=str(e))
            return []

    async def _fetch_github_issues(self,
                                    query: str,
                                    repo: str) -> list[dict]:
        """Search GitHub issues on relevant open-source repos (public API)."""
        url = "https://api.github.com/search/issues"
        params = {
            "q":        f"{query} repo:{repo} type:issue",
            "per_page": 5,
            "sort":     "relevance",
        }
        headers = {"Accept": "application/vnd.github.v3+json"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    url, params=params, headers=headers)
                resp.raise_for_status()
                items = resp.json().get("items", [])
                results = []
                for i in items:
                    results.append({
                        "id":          f"#{i['number']}",
                        "title":       i["title"],
                        "url":         i["html_url"],
                        "status":      i["state"],
                        "source":      "github",
                        "description": (i.get("body") or "")[:300],
                        "excerpt":     (i.get("body") or "")[:200],
                        "relevance":   "medium",
                    })
                log.info("GitHub Issues search",
                         repo=repo, query=query,
                         count=len(results))
                return results
        except Exception as e:
            log.warning("GitHub Issues search failed",
                        repo=repo, error=str(e))
            return []

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
                # Iteration 0: deterministic initial search,
                # run Confluence + Stack Overflow in parallel
                if iteration == 0:
                    query = initial_query
                    messages.append({
                        "role": "assistant",
                        "content": (
                            f"Thought: Starting with "
                            f"deterministic query.\n"
                            f"Action: search_confluence\n"
                            f"Action Input: {query}"),
                    })
                    conf_r, so_r = await asyncio.gather(
                        self._search_confluence(
                            query, target_space),
                        self._fetch_stack_overflow(query),
                        return_exceptions=True,
                    )
                    if isinstance(conf_r, Exception):
                        log.warning(
                            "Confluence failed on iteration 0",
                            error=str(conf_r))
                        conf_r = []
                    if isinstance(so_r, Exception):
                        log.warning(
                            "StackOverflow failed on iteration 0",
                            error=str(so_r))
                        so_r = []
                    # Tag sources explicitly
                    for item in conf_r:
                        item["source"] = "confluence"
                    for item in so_r:
                        item["source"] = "stackoverflow"
                    self._iter0_so_results = so_r
                    results = conf_r
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
                    continue

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
            target_space: str = None) -> list[dict]:
        try:
            connectors = (
                await ConnectorRegistry.get_all_by_type("confluence")
                + await ConnectorRegistry.get_all_by_type("support_kb")
            )
            if not connectors:
                try:
                    all_c = await ConnectorRegistry.get_all_enabled()
                    connectors = [
                        c for c in all_c
                        if getattr(c, "is_knowledge_source", False)
                    ]
                except Exception:
                    return []

            if not connectors:
                return []

            # Find connector matching target space
            # Fall back to first confluence connector
            target_connector = None
            if target_space:
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
                    "source":    target_connector.system_type,
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

    async def _fetch_stack_overflow(self,
                                     query: str,
                                     max_results: int = 5
                                     ) -> list[dict]:
        try:
            url    = "https://api.stackexchange.com/2.3/search/advanced"
            params = {
                "q":        query,
                "site":     "stackoverflow",
                "pagesize": max_results,
                "order":    "desc",
                "sort":     "relevance",
                "filter":   "withbody",
            }
            async with httpx.AsyncClient(
                    timeout=5,
                    follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    log.warning("StackOverflow advanced search failed",
                                status=resp.status_code,
                                query=query)
                    return []
                items   = resp.json().get("items", [])
                results = []
                for item in items:
                    if item.get("answer_count", 0) < 1:
                        continue
                    body    = item.get("body", "")
                    excerpt = re.sub(r'<[^>]+>', '', body)[:300]
                    results.append({
                        "title":        item.get("title", ""),
                        "url":          item.get("link", ""),
                        "score":        item.get("score", 0),
                        "answer_count": item.get(
                            "answer_count", 0),
                        "excerpt":      excerpt,
                        "source":       "stackoverflow",
                    })
                log.info("StackOverflow advanced search",
                         query=query, count=len(results))
                return results[:max_results]
        except Exception as e:
            log.warning("StackOverflow advanced search error",
                        query=query, error=str(e))
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
