import re
import httpx
from .base_connector import BaseConnector
from ..models.ticket import TicketData, ChangeEvent


SEVERITY_LABEL_MAP = {
    "priority:blocker": "P0",
    "priority:critical": "P0",
    "priority:high": "P1",
    "priority:medium": "P2",
    "priority:low": "P3",
    "bug": "P2",
    "enhancement": "P3",
}

SKIP_LABELS = {
    "bug", "enhancement", "question", "good first issue",
    "help wanted", "wontfix", "duplicate", "invalid",
}


class GithubConnector(BaseConnector):
    def _headers(self) -> dict:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _repo(self) -> str:
        return self.project_key

    def _extract_priority_from_labels(self, labels: list) -> str:
        for lbl in labels:
            lbl_lower = lbl.lower()
            if lbl_lower in SEVERITY_LABEL_MAP:
                return SEVERITY_LABEL_MAP[lbl_lower]
        for lbl in labels:
            lbl_lower = lbl.lower()
            for key, val in SEVERITY_LABEL_MAP.items():
                if key in lbl_lower:
                    return val
        return "Unknown"

    def _normalise(self, raw: dict) -> TicketData:
        labels = [lbl.get("name", "") for lbl in raw.get("labels", [])]
        severity = self._extract_priority_from_labels(labels)

        component = ""
        for lbl in labels:
            if lbl.lower() not in SKIP_LABELS:
                component = lbl
                break

        body = raw.get("body") or ""
        linked = re.findall(r"#(\d+)", body)
        linked_items = [{"id": num, "type": "issue_ref", "title": ""} for num in linked[:10]]

        return TicketData(
            ticket_id=str(raw.get("number", "")),
            title=raw.get("title", ""),
            description=body[:2000],
            severity=severity,
            status="Open" if raw.get("state") == "open" else "Closed",
            component=component,
            assignee=((raw.get("assignee") or {}).get("login") or ""),
            reporter=((raw.get("user") or {}).get("login") or ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            source_id=self.source_id,
            system_type=self.system_type,
            url=raw.get("html_url", ""),
            api_url=raw.get("url", ""),
            labels=labels,
            linked_items=linked_items,
        )

    async def get(self, ticket_id: str) -> TicketData | None:
        url = f"{self.base_url}/repos/{self._repo()}/issues/{ticket_id}"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                raw_data = resp.json()
                ticket = self._normalise(raw_data)
                ticket.direct_reference_links = self.extract_links(raw_data)
                return ticket
        except Exception:
            return None

    async def search(self, query: str, max_results: int = 300, page: int = 1) -> list[TicketData]:
        if query:
            url = f"https://api.github.com/search/issues"
            params = {"q": f"{query}+repo:{self._repo()}+is:issue+is:open", "per_page": min(max_results, 100), "page": page}
        else:
            url = f"https://api.github.com/repos/{self._repo()}/issues"
            params = {"state": "open", "per_page": min(max_results, 100), "page": page, "sort": "updated", "direction": "desc"}

        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                items = data.get("items", data) if query else data
                if not isinstance(items, list):
                    return []
                return [self._normalise(i) for i in items if i.get("pull_request") is None]
        except Exception:
            return []

    async def get_linked_items(self, ticket_id: str) -> list[dict]:
        ticket = await self.get(ticket_id)
        if ticket:
            return ticket.linked_items
        return []

    async def get_lightweight(self, ticket_id: str) -> dict:
        url = f"{self.base_url}/repos/{self._repo()}/issues/{ticket_id}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    return {}
                data = resp.json()
                labels = [lbl.get("name", "") for lbl in data.get("labels", [])]
                severity = "Unknown"
                for lbl in labels:
                    if lbl.lower() in SEVERITY_LABEL_MAP:
                        severity = SEVERITY_LABEL_MAP[lbl.lower()]
                        break
                return {
                    "updated_at": data.get("updated_at", ""),
                    "severity": severity,
                    "status": "Open" if data.get("state") == "open" else "Closed",
                }
        except Exception:
            return {}

    def extract_links(self, raw_payload: dict) -> list[dict]:
        import re
        links = []
        body = raw_payload.get("body") or ""
        repo = self._repo()  # e.g. "apache/spark"

        # 1. External URLs in body (JIRA, Bugzilla)
        for url in re.findall(
                r'https?://[^\s<>"]+', body):
            # JIRA issue URL pattern
            if "issues.apache.org" in url or "jira." in url:
                m = re.search(r'/browse/([A-Z]{2,10}-\d+)', url)
                if m:
                    links.append({
                        "raw_id": m.group(1),
                        "source": "JIRA",
                        "relationship": "Linked Reference",
                        "url": url,
                    })
            # Bugzilla URL pattern
            elif "bugzilla" in url and "id=" in url:
                bz_id = url.split("id=")[-1].split("&")[0]
                if bz_id.isdigit():
                    links.append({
                        "raw_id": bz_id,
                        "source": "Bugzilla",
                        "relationship": "See Also",
                        "url": url,
                    })

        # 2. Internal issue/PR references: "Closes #22378",
        #    "Fixes #1234", "Related to #5678", "#9012"
        for match in re.finditer(
                r'(?:Closes?|Fixes?|Resolves?|Related\s+to'
                r'|See\s+also|dup\s+of|duplicate\s+of)?\s*'
                r'#(\d{3,6})\b',
                body, re.IGNORECASE):
            ref_id = match.group(1)
            issue_num = str(raw_payload.get("number", ""))
            if ref_id != issue_num:
                links.append({
                    "raw_id": ref_id,
                    "source": "GitHub",
                    "relationship": "Mentioned Issue/PR",
                })

        # Deduplicate
        seen = set()
        unique = []
        for l in links:
            if l["raw_id"] not in seen:
                seen.add(l["raw_id"])
                unique.append(l)
        return unique

    async def get_changelog(self, ticket_id: str, since: str = "") -> list[ChangeEvent]:
        url = f"{self.base_url}/repos/{self._repo()}/issues/{ticket_id}/events"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                events = resp.json()
                changes = []
                for ev in events:
                    created = ev.get("created_at", "")
                    if since and created <= since:
                        continue
                    changes.append(ChangeEvent(
                        field=ev.get("event", ""),
                        old_value="",
                        new_value=str(ev.get("label", {}).get("name", "") if ev.get("label") else ""),
                        changed_at=created,
                        changed_by=(ev.get("actor") or {}).get("login", ""),
                    ))
                return changes
        except Exception:
            return []
