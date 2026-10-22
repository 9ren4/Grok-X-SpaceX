"""The shared contract. Every module reads and writes EventSpec."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Venue(BaseModel):
    name: str = ""
    address: str = ""
    city: str = ""
    capacity: int | None = None
    contact: str = ""


class LineItem(BaseModel):
    label: str
    qty: float = 1
    unit_cost: float = 0.0
    currency: str = "GBP"
    note: str = ""
    source: str = ""  # "memory:<event>" when it came from the knowledge base

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_cost, 2)


class Slot(BaseModel):
    time: str  # "18:30"
    duration_min: int = 30
    item: str
    owner: str = ""


class Shift(BaseModel):
    role: str
    volunteer: str = ""
    start: str = ""
    end: str = ""


class Person(BaseModel):
    name: str
    role: str = ""


class EventSpec(BaseModel):
    id: str = ""
    title: str = ""
    one_liner: str = ""
    description: str = ""
    date: datetime | None = None
    start: str = ""
    end: str = ""
    timezone: str = "Europe/London"
    venue: Venue = Field(default_factory=Venue)
    expected_attendees: int = 0
    audience: str = ""
    format: Literal["meetup", "workshop", "hackathon", "social", "talk"] = "meetup"
    hosts: list[Person] = Field(default_factory=list)
    ticket_price: float | None = None
    currency: str = "GBP"
    luma_url: str = ""
    cover_url: str = ""

    # Produced by the planner
    run_sheet: list[Slot] = Field(default_factory=list)
    budget: list[LineItem] = Field(default_factory=list)
    rota: list[Shift] = Field(default_factory=list)

    missing: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def budget_total(self) -> float:
        return round(sum(i.total for i in self.budget), 2)

    @property
    def per_head(self) -> float:
        n = self.expected_attendees or 1
        return round(self.budget_total / n, 2)


class Memory(BaseModel):
    """One fact in the knowledge base."""
    kind: str  # budget | venue | supplier | voice | volunteer | lesson
    text: str
    tags: list[str] = Field(default_factory=list)
    provenance: str = ""  # which event taught us this
    score: float = 0.0


class Entity(BaseModel):
    kind: str  # venue | supplier | sponsor | volunteer | community | event
    name: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    times_used: int = 1


class Relation(BaseModel):
    source: str  # "kind:name"
    relation: str
    target: str
    event_id: str = ""


class Artifacts(BaseModel):
    """The copy outputs."""
    page_copy: str = ""
    newsletter_subject: str = ""
    newsletter_html: str = ""
    linkedin: str = ""
    whatsapp: str = ""


class RunResult(BaseModel):
    spec: EventSpec
    artifacts: Artifacts
    memories_used: list[Memory] = Field(default_factory=list)
    learned: int = 0
    hours_saved: float = 0.0
    log: list[str] = Field(default_factory=list)
    ghost_url: str = ""
