"""L.Y.R.A v3 — Kernel data models.

Defines the core Pydantic models used across the entire system:
events, facts, messages, enums, and health status.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────

class Priority(str, Enum):
    """Event priority levels."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SpeedTag(str, Enum):
    """Tags that define how fast / where a message is dispatched."""
    INSTANT = "[I]"
    CONFIRM_FIRE = "[CF]"
    BACKGROUND = "[BG]"
    AUTONOME = "[BG:PROJECT]"
    VOIX = "[voix]"


class ApprovalLevel(str, Enum):
    """Approval level for tool execution."""
    ALWAYS = "ALWAYS"
    ASK = "ASK"
    NEVER = "NEVER"


# ── Core models ────────────────────────────────────────────────────────────────

class Event(BaseModel):
    """A named event with a payload, priority and metadata."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = Priority.MEDIUM
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str = "system"


class Fact(BaseModel):
    """A semantic fact stored in the knowledge graph / memory."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sujet: str
    predicat: str
    objet: str
    categorie: str = "general"
    statut: str = "active"
    confiance: float = 0.5
    importance: float = 0.5
    timestamp: datetime = Field(default_factory=datetime.now)


class Message(BaseModel):
    """A user or system message that flows through the pipeline."""
    role: str = "user"
    content: str
    tags: list[SpeedTag] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result of a tool execution."""
    success: bool
    output: str = ""
    error: Optional[str] = None
    tool_name: str
    duration_ms: float = 0.0


class HealthStatus(BaseModel):
    """Health-check result for a single component."""
    component: str
    alive: bool
    last_check: Optional[datetime] = None
    error: Optional[str] = None
