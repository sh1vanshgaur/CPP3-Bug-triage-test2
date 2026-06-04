import re
import base64
import os
import httpx
import structlog
from .base_connector import BaseConnector
from ..models.ticket import TicketData, ChangeEvent

log = structlog.get_logger()


class ConfluenceConnector(BaseConnector):

    # Known correct base URLs for common confluence instances
    KNOWN_BASE_URLS = {
        "cwiki.apache.org":         "https://cwiki.apache.org/confluence",
        "cpp3-hpe.atlassian.net":   "https://cpp3-hpe.atlassian.net/wiki",
    }

    MOCK_DOMAINS = [
        "confluence.example.com",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "example.com",
    ]

    def _headers(self) -> dict:
        """
        Build auth headers.
        - If email + token: Basic Auth (Atlassian Cloud)
        - If token only: Bearer Auth (PAT / server)
        - If neither: no auth header (public wiki)
        """
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json"}
        email = os.getenv("CONFLUENCE_EMAIL", "").strip()
        token = (self.token or "").strip()

        if email and token:
            creds = base64.b64encode(
                f"{email}:{token}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        # else: public wiki — no Authorization header
        return headers

    def _strip_html(self, text: str) -> str:
        """
        Remove HTML/XHTML tags, decode entities,
        normalize whitespace, cap at 2000 chars.
        """
        import html
        # Remove HTML tags (non-greedy)
        clean = re.sub(r'<[^>]+?>', ' ', text or '')
        # Decode all HTML entities
        clean = html.unescape(clean)
        # Normalize whitespace
        clean = re.sub(r'[\s\t\n\r]+', ' ', clean).strip()
        return clean[:2000]

    def _build_url(self, webui_path: str,
                   links_base: str = "") -> str:
        bad_domains = [
            "confluence.example.com",
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "example.com",
        ]

        corp_url = os.getenv("CONFLUENCE_URL", "https://cpp3-hpe.atlassian.net/wiki").rstrip("/")

        # Best case: API gave us the base URL directly
        # links_base + webui_path is always the correct URL
        if links_base and webui_path:
            clean_base = links_base.rstrip("/")
            if any(b in clean_base for b in bad_domains):
                clean_base = corp_url
            path = webui_path.strip()
            if not path.startswith("/"):
                path = "/" + path
            if clean_base.endswith("/wiki") and path.startswith("/wiki"):
                return f"{clean_base}{path[5:]}"
            return f"{clean_base}{path}"

        # Fallback: reconstruct from self.base_url
        base = self.base_url.rstrip("/")
        # Sanitize mock base
        if any(b in base for b in bad_domains):
            base = corp_url

        if not webui_path:
            return base

        # Already a full URL
        if webui_path.startswith("http"):
            if not any(b in webui_path for b in bad_domains):
                return webui_path
            try:
                from urllib.parse import urlparse
                webui_path = urlparse(webui_path).path
            except Exception:
                return base

        path = webui_path.strip()
        if not path.startswith("/"):
            path = "/" + path

        # Apache: base already has /confluence
        if "cwiki.apache.org" in base:
            if path.startswith("/confluence"):
                return f"https://cwiki.apache.org{path}"
            return f"{base}{path}"

        # Atlassian Cloud: strip /wiki from path
        if path.startswith("/wiki"):
            return f"{base}{path[5:]}"

        return f"{base}{path}"

    async def search(self, query: str,
                     max_results: int = 5) -> list[TicketData]:
        space = (self.project_key or "HPEKB").strip()

        if query and query.strip():
            # Escape quotes in query for CQL safety
            safe_query = query.replace('"', '\\"')
            cql = (f'space = "{space}" AND '
                   f'text ~ "{safe_query}" AND type = page '
                   f'ORDER BY lastModified DESC')
        else:
            cql = (f'space = "{space}" AND type = page '
                   f'ORDER BY lastModified DESC')

        url = f"{self.base_url.rstrip('/')}/rest/api/content/search"
        params = {
            "cql": cql,
            "limit": min(max_results, 10),
            "expand": "body.storage,version",
        }

        try:
            async with httpx.AsyncClient(
                    timeout=20,
                    follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params=params)

                if resp.status_code == 401:
                    log.warning("Confluence auth failed",
                                source=self.source_id,
                                url=url)
                    return []
                if resp.status_code != 200:
                    log.warning("Confluence search failed",
                                status=resp.status_code,
                                source=self.source_id,
                                query=query)
                    return []

                results = resp.json().get("results") or []
                tickets = []

                for r in results:
                    try:
                        body_raw = (r.get("body", {})
                                      .get("storage", {})
                                      .get("value", ""))
                        description = self._strip_html(body_raw)
                        version = r.get("version") or {}
                        when = version.get("when", "")
                        links    = r.get("_links") or {}
                        webui    = links.get("webui", "")
                        api_base = links.get("base", "")
                        full_url = self._build_url(webui, api_base)

                        tickets.append(TicketData(
                            ticket_id=str(r.get("id", "")),
                            title=r.get("title", ""),
                            description=description,
                            severity="Unknown",
                            status="Published",
                            component=space,
                            assignee="",
                            reporter="",
                            created_at="",
                            updated_at=when,
                            source_id=self.source_id,
                            system_type=self.system_type,
                            url=full_url,
                        ))
                    except Exception as e:
                        log.warning("Confluence result parse error",
                                    error=str(e))
                        continue

                log.info("Confluence search complete",
                         source=self.source_id,
                         space=space,
                         query=query,
                         count=len(tickets))
                return tickets

        except httpx.TimeoutException:
            log.warning("Confluence search timeout",
                        source=self.source_id, query=query)
            return []
        except Exception as e:
            log.warning("Confluence search error",
                        source=self.source_id, error=str(e))
            return []

    async def get(self, article_id: str) -> TicketData | None:
        url = (f"{self.base_url.rstrip('/')}"
               f"/rest/api/content/{article_id}"
               f"?expand=body.storage,version")
        try:
            async with httpx.AsyncClient(
                    timeout=15,
                    follow_redirects=True) as client:
                resp = await client.get(
                    url, headers=self._headers())
                if resp.status_code != 200:
                    return None
                r = resp.json()
                body_raw = (r.get("body", {})
                              .get("storage", {})
                              .get("value", ""))
                links    = r.get("_links") or {}
                webui    = links.get("webui", "")
                api_base = links.get("base", "")
                return TicketData(
                    ticket_id=str(r.get("id", "")),
                    title=r.get("title", ""),
                    description=self._strip_html(body_raw),
                    severity="Unknown",
                    status="Published",
                    component=self.project_key or "",
                    assignee="",
                    reporter="",
                    created_at="",
                    updated_at="",
                    source_id=self.source_id,
                    system_type=self.system_type,
                    url=self._build_url(webui, api_base),
                )
        except Exception as e:
            log.warning("Confluence get error",
                        article_id=article_id, error=str(e))
            return None

    async def get_linked_items(self, ticket_id: str) -> list:
        return []

    async def get_changelog(self, ticket_id: str,
                             since: str = "") -> list[ChangeEvent]:
        return []

    async def get_lightweight(self, ticket_id: str) -> dict:
        return {}

    def extract_links(self, raw_payload: dict) -> list[dict]:
        return []
