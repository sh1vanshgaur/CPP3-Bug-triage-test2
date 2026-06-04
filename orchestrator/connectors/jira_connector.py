import httpx
from .base_connector import BaseConnector
from ..models.ticket import TicketData, ChangeEvent

JIRA_STATUS_MAP = {
    "open": "Open",
    "reopened": "Open",
    "in progress": "In Progress",
    "resolved": "Resolved",
    "closed": "Closed",
    "done": "Closed",
}
JIRA_PRIORITY_MAP = {
    "blocker": "P0",
    "critical": "P0",
    "p0": "P0",
    "high": "P1",
    "p1": "P1",
    "medium": "P2",
    "p2": "P2",
    "normal": "P2",
    "low": "P3",
    "p3": "P3",
    "trivial": "P3",
    "minor": "P3",
}
class JiraConnector(BaseConnector):
    def _extract_text_from_adf(self, content) -> str:
        """Recursively extract plain text from Atlassian Document Format (ADF)."""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if content.get("type") == "text":
                return content.get("text", "")
            parts = [self._extract_text_from_adf(child) for child in content.get("content", [])]
            return " ".join(filter(None, parts))
        if isinstance(content, list):
            return " ".join(self._extract_text_from_adf(item) for item in content)
        return ""

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _normalise(self, raw: dict) -> TicketData:
        fields = raw.get("fields") or {}

        priority_name = ((fields.get("priority") or {}).get("name") or "").lower()
        severity = JIRA_PRIORITY_MAP.get(priority_name, "Unknown")

        raw_status = ((fields.get("status") or {}).get("name") or "").lower()
        status = JIRA_STATUS_MAP.get(raw_status, raw_status.title() if raw_status else "Unknown")

        components = fields.get("components") or []
        component = components[0].get("name", "") if components else ""

        assignee = ((fields.get("assignee") or {}).get("displayName") or "")
        reporter = ((fields.get("reporter") or {}).get("displayName") or "")

        raw_comments = ((fields.get("comment") or {}).get("comments") or [])
        comments = [
            {
                "author": (c.get("author") or {}).get("displayName", ""),
                "body": (c.get("body") or "")[:500],
                "created": c.get("created", ""),
            }
            for c in raw_comments[-5:]
        ]

        raw_links = fields.get("issuelinks") or []
        linked_items = []
        for lnk in raw_links:
            inward = lnk.get("inwardIssue") or {}
            outward = lnk.get("outwardIssue") or {}
            target = inward or outward
            if target.get("key"):
                linked_items.append({
                    "id": target["key"],
                    "type": (lnk.get("type") or {}).get("name", "relates"),
                    "title": (target.get("fields") or {}).get("summary", ""),
                })

        raw_description = fields.get("description") or ""
        if isinstance(raw_description, dict):
            # Atlassian Document Format (Jira Cloud v3)
            description = self._extract_text_from_adf(raw_description)
        elif isinstance(raw_description, str):
            description = raw_description
        else:
            description = str(raw_description) if raw_description else ""
        description = description[:2000]

        return TicketData(
            ticket_id=raw.get("key", ""),
            title=(fields.get("summary") or ""),
            description=description,
            severity=severity,
            status=status,
            component=component,
            assignee=assignee,
            reporter=reporter,
            created_at=(fields.get("created") or ""),
            updated_at=(fields.get("updated") or ""),
            source_id=self.source_id,
            system_type=self.system_type,
            url=f"{self.base_url}/browse/{raw.get('key', '')}",
            comments=comments,
            linked_items=linked_items,
        )

    async def get(self, ticket_id: str) -> TicketData | None:
        fields = "summary,description,status,priority,components,assignee,reporter,created,updated,comment,issuelinks"
        url = f"{self.base_url}/rest/api/2/issue/{ticket_id}"
        params = {"fields": fields, "expand": "changelog"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                raw_data = resp.json()
                ticket = self._normalise(raw_data)
                ticket.direct_reference_links = self.extract_links(raw_data)
                return ticket
        except Exception:
            return None

    async def search(self, query: str, max_results: int = 100, start_at: int = 0) -> list[TicketData]:
        fields = ["summary", "description", "status", "priority", "components",
                  "assignee", "reporter", "created", "updated", "comment", "issuelinks"]
        if query:
            jql = f'project = {self.project_key} AND text ~ "{query}" ORDER BY updated DESC'
        else:
            jql = f'project = {self.project_key} AND statusCategory in ("To Do", "In Progress") ORDER BY updated DESC'

        payload = {
            "jql": jql,
            "maxResults": min(max_results, 100),
            "startAt": start_at,
            "fields": fields,
        }
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"{self.base_url}/rest/api/2/search",
                    json=payload, headers=self._headers()
                )
                resp.raise_for_status()
                issues = resp.json().get("issues") or []
                return [self._normalise(issue) for issue in issues if isinstance(issue, dict)]
        except Exception:
            return []

    async def get_lightweight(self, ticket_id: str) -> dict:
        url = f"{self.base_url}/rest/api/2/issue/{ticket_id}"
        params = {"fields": "updated,priority,status"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code != 200:
                    return {}
                fields = resp.json().get("fields", {})
                priority_name = ((fields.get("priority") or {}).get("name") or "").lower()
                severity = JIRA_PRIORITY_MAP.get(priority_name, "Unknown")
                raw_status = ((fields.get("status") or {}).get("name") or "").lower()
                status = JIRA_STATUS_MAP.get(raw_status, raw_status.title())
                return {"updated_at": fields.get("updated", ""), "severity": severity, "status": status}
        except Exception:
            return {}

    async def get_linked_items(self, ticket_id: str) -> list[dict]:
        ticket = await self.get(ticket_id)
        if ticket:
            return ticket.linked_items
        return []

    def extract_links(self, raw_payload: dict) -> list[dict]:
        links = []
        fields = raw_payload.get("fields") or {}

        # 1. Direct issue-to-issue links (JIRA issuelinks block)
        for link in (fields.get("issuelinks") or []):
            sub = link.get("outwardIssue") or link.get("inwardIssue")
            if sub:
                links.append({
                    "raw_id": sub.get("key", ""),
                    "source": "JIRA",
                    "relationship": (
                        (link.get("type") or {}).get("name",
                                                      "referenced")),
                })

        # 2. Remote web links — GitHub PRs and external trackers
        for rlink in (fields.get("remotelinks") or []):
            url = ((rlink.get("object") or {}).get("url") or "")
            if not url:
                continue
            # GitHub PR pattern
            if "github.com" in url and "/pull/" in url:
                try:
                    pr_id = url.rstrip("/").split("/")[-1]
                    if pr_id.isdigit():
                        links.append({
                            "raw_id": pr_id,
                            "source": "GitHub",
                            "relationship": "Pull Request",
                            "url": url,
                        })
                except Exception:
                    pass
            # GitHub issue pattern
            elif "github.com" in url and "/issues/" in url:
                try:
                    issue_id = url.rstrip("/").split("/")[-1]
                    if issue_id.isdigit():
                        links.append({
                            "raw_id": issue_id,
                            "source": "GitHub",
                            "relationship": "Issue Reference",
                            "url": url,
                        })
                except Exception:
                    pass
            # Bugzilla pattern
            elif "bugzilla" in url and "id=" in url:
                try:
                    bz_id = url.split("id=")[-1].split("&")[0]
                    if bz_id.isdigit():
                        links.append({
                            "raw_id": bz_id,
                            "source": "Bugzilla",
                            "relationship": "See Also",
                            "url": url,
                        })
                except Exception:
                    pass

        # 3. Scan description text for JIRA ticket IDs
        desc = str(fields.get("description") or "")
        import re
        for match in re.finditer(
                r'\b([A-Z]{2,10}-\d{3,6})\b', desc):
            raw_id = match.group(1)
            if raw_id != raw_payload.get("key", ""):
                links.append({
                    "raw_id": raw_id,
                    "source": "JIRA",
                    "relationship": "Mentioned",
                })

        # Deduplicate by raw_id
        seen = set()
        unique = []
        for l in links:
            if l["raw_id"] not in seen:
                seen.add(l["raw_id"])
                unique.append(l)
        return unique

    async def get_changelog(self, ticket_id: str, since: str = "") -> list[ChangeEvent]:
        url = f"{self.base_url}/rest/api/2/issue/{ticket_id}/changelog"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                values = data.get("values") or data.get("histories") or []
                changes = []
                for entry in values:
                    created = entry.get("created", "")
                    if since and created <= since:
                        continue
                    author = (entry.get("author") or {}).get("displayName", "")
                    for item in entry.get("items") or []:
                        changes.append(ChangeEvent(
                            field=item.get("field", ""),
                            old_value=item.get("fromString") or "",
                            new_value=item.get("toString") or "",
                            changed_at=created,
                            changed_by=author,
                        ))
                return changes
        except Exception:
            return []
