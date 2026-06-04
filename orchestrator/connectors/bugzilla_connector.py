import httpx
from .base_connector import BaseConnector
from ..models.ticket import TicketData, ChangeEvent

BZ_STATUS_MAP = {
    "UNCONFIRMED": "Open",
    "NEW": "Open",
    "ASSIGNED": "In Progress",
    "IN_PROGRESS": "In Progress",
    "RESOLVED": "Resolved",
    "VERIFIED": "Resolved",
    "CLOSED": "Closed",
}

BZ_PRIORITY_MAP = {
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "critical": "P0",
    "blocker": "P0",
    "major": "P1",
    "normal": "P2",
    "minor": "P3",
    "enhancement": "P3",
    "--": "Unknown",
}


class BugzillaConnector(BaseConnector):
    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _normalise(self, raw: dict) -> TicketData:
        raw_status = raw.get("status", "").upper()
        status = BZ_STATUS_MAP.get(raw_status, "Open")

        priority_raw = raw.get("priority", "--")
        severity_raw = raw.get("severity", "--")
        severity = BZ_PRIORITY_MAP.get(priority_raw, None) or BZ_PRIORITY_MAP.get(severity_raw, "Unknown")

        see_also = raw.get("see_also") or []
        linked_items = [{"id": str(s), "type": "see_also", "title": ""} for s in see_also]

        description = raw.get("description", "") or ""

        return TicketData(
            ticket_id=str(raw.get("id", "")),
            title=raw.get("summary", ""),
            description=str(description)[:2000],
            severity=severity,
            status=status,
            component=raw.get("component", ""),
            assignee=raw.get("assigned_to", ""),
            reporter=raw.get("creator", ""),
            created_at=raw.get("creation_time", ""),
            updated_at=raw.get("last_change_time", ""),
            source_id=self.source_id,
            system_type=self.system_type,
            url=f"{self.base_url}/show_bug.cgi?id={raw.get('id', '')}",
            linked_items=linked_items,
        )

    async def get(self, ticket_id: str) -> TicketData | None:
        url = f"{self.base_url}/rest/bug/{ticket_id}"
        params = {
            "include_fields": "id,summary,status,priority,severity,component,assigned_to,creator,creation_time,last_change_time,see_also,description"
        }
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                raw_data = resp.json()
                bugs = raw_data.get("bugs") or []
                if not bugs:
                    return None
                ticket = self._normalise(bugs[0])
                ticket.direct_reference_links = self.extract_links(raw_data)
                return ticket
        except Exception:
            return None

    async def search(self, query: str, max_results: int = 300) -> list[TicketData]:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                if not query:
                    url = f"{self.base_url}/rest/bug"
                    params = [
                        ("product", self.project_key),
                        ("status", "UNCONFIRMED"),
                        ("status", "NEW"),
                        ("status", "ASSIGNED"),
                        ("status", "REOPENED"),
                        ("limit", str(max_results)),
                        ("order", "changeddate DESC"),
                        ("include_fields", "id,summary,status,priority,severity,component,assigned_to,creator,creation_time,last_change_time,see_also"),
                    ]
                else:
                    url = f"{self.base_url}/rest/bug"
                    params = [
                        ("product", self.project_key),
                        ("quicksearch", query),
                        ("limit", str(max_results)),
                        ("include_fields", "id,summary,status,priority,severity,component,assigned_to,creator,creation_time,last_change_time,see_also"),
                    ]
                resp = await client.get(url, headers=self._headers(), params=params)
                resp.raise_for_status()
                bugs = resp.json().get("bugs") or []
                return [self._normalise(bug) for bug in bugs if isinstance(bug, dict)]
        except Exception as e:
            print(f"[Bugzilla] search failed: {e}")
            return []

    async def get_linked_items(self, ticket_id: str) -> list[dict]:
        ticket = await self.get(ticket_id)
        if ticket:
            return ticket.linked_items
        return []

    async def get_lightweight(self, ticket_id: str) -> dict:
        url = f"{self.base_url}/rest/bug/{ticket_id}"
        params = {"include_fields": "last_change_time,priority,status"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code != 200:
                    return {}
                bugs = resp.json().get("bugs", [])
                if not bugs:
                    return {}
                b = bugs[0]
                return {
                    "updated_at": b.get("last_change_time", ""),
                    "severity": "Unknown",
                    "status": b.get("status", ""),
                }
        except Exception:
            return {}

    def extract_links(self, raw_payload: dict) -> list[dict]:
        import re
        links = []
        bugs = raw_payload.get("bugs") or []
        if not bugs:
            return []
        bug = bugs[0]

        # 1. depends_on and blocks — direct Bugzilla dependencies
        for dep_id in (bug.get("depends_on") or []):
            links.append({
                "raw_id": str(dep_id),
                "source": "Bugzilla",
                "relationship": "Depends On",
            })
        for block_id in (bug.get("blocks") or []):
            links.append({
                "raw_id": str(block_id),
                "source": "Bugzilla",
                "relationship": "Blocks",
            })

        # 2. see_also — external URLs
        for url in (bug.get("see_also") or []):
            if "issues.apache.org" in url or "jira." in url:
                m = re.search(r'/browse/([A-Z]{2,10}-\d+)', url)
                if m:
                    links.append({
                        "raw_id": m.group(1),
                        "source": "JIRA",
                        "relationship": "See Also",
                        "url": url,
                    })
            elif "github.com" in url and "/issues/" in url:
                issue_id = url.rstrip("/").split("/")[-1]
                if issue_id.isdigit():
                    links.append({
                        "raw_id": issue_id,
                        "source": "GitHub",
                        "relationship": "See Also",
                        "url": url,
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
        url = f"{self.base_url}/rest/bug/{ticket_id}/history"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                history = data.get("bugs") or []
                changes = []
                for bug in history:
                    for entry in bug.get("history") or []:
                        when = entry.get("when", "")
                        if since and when <= since:
                            continue
                        who = entry.get("who", "")
                        for ch in entry.get("changes") or []:
                            changes.append(ChangeEvent(
                                field=ch.get("field_name", ""),
                                old_value=ch.get("removed", ""),
                                new_value=ch.get("added", ""),
                                changed_at=when,
                                changed_by=who,
                            ))
                return changes
        except Exception:
            return []
