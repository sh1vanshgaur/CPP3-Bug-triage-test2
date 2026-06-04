from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TicketData:
    ticket_id: str
    title: str
    description: str
    severity: str
    status: str
    component: str
    assignee: str
    reporter: str
    created_at: str
    updated_at: str
    source_id: str
    system_type: str
    url: str = ""
    error_excerpt: str = ""
    comments: list = field(default_factory=list)
    linked_items: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    direct_reference_links: list = field(default_factory=list)


@dataclass
class ChangeEvent:
    field: str
    old_value: str
    new_value: str
    changed_at: str
    changed_by: str = ""
