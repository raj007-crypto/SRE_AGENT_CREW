"""
Shared state object that flows through every agent in the pipeline.

Each agent reads what it needs from this object and writes its own
section back. This is the single source of truth for one incident run,
and it's what gets persisted so a run can pause (approval gate) and
resume later.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    ROOT_CAUSE_FOUND = "root_cause_found"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    REMEDIATED = "remediated"
    DOCUMENTED = "documented"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Stage 1 output: Detector
# ---------------------------------------------------------------------------

class Alert(BaseModel):
    """The raw signal that kicked off this incident (from a webhook)."""
    service: str
    alert_type: str          # e.g. "error_rate_spike", "latency_spike"
    metric_name: str
    threshold_breached: float
    current_value: float
    triggered_at: datetime


# ---------------------------------------------------------------------------
# Stage 2 output: Investigator
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    service: str
    message: str


class MetricPoint(BaseModel):
    timestamp: datetime
    metric_name: str
    value: float


class TraceSpan(BaseModel):
    timestamp: datetime
    service: str
    operation: str
    duration_ms: float
    error: bool = False


class Evidence(BaseModel):
    logs: list[LogEntry] = Field(default_factory=list)
    metrics: list[MetricPoint] = Field(default_factory=list)
    traces: list[TraceSpan] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 3 output: Root-cause agent
# ---------------------------------------------------------------------------
# SO the class down tells us about the recent deploy that has
#been done and if there is any error from that deployment or not
class DeployEvent(BaseModel): 
    commit_sha: str #stores the git id of the last commit
    author: str
    message: str
    deployed_at: datetime


class Hypothesis(BaseModel):#stores the probable cause of the error
    probable_cause: str
    confidence: float               # 0.0 - 1.0
    suspect_deploy: Optional[DeployEvent] = None
    reasoning: str


# ---------------------------------------------------------------------------
# Stage 4 output: Remediator + approval gate
# ---------------------------------------------------------------------------

class RemediationProposal(BaseModel):#The proposed fix: what action to take, what it targets, and why.
    action: str                     # e.g. "rollback"
    target: str                     # e.g. commit sha or service name
    justification: str


class ApprovalDecision(BaseModel):#this tells us the human decision
    #who approved the decision and time and everything
    approved: bool
    approved_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    note: Optional[str] = None

#what actually happened when the action was carried out
class RemediationResult(BaseModel):
    executed: bool
    output: str
    executed_at: datetime


# ---------------------------------------------------------------------------
# Stage 5 output: Scribe
# ---------------------------------------------------------------------------
#the final postmortem
class Postmortem(BaseModel):
    title: str
    markdown: str
    generated_at: datetime


# ---------------------------------------------------------------------------
# The object that threads through the whole graph
# ---------------------------------------------------------------------------

class Incident(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    status: IncidentStatus = IncidentStatus.DETECTED
    created_at: datetime = Field(default_factory=datetime.utcnow)

    alert: Alert
    evidence: Optional[Evidence] = None
    hypothesis: Optional[Hypothesis] = None
    proposal: Optional[RemediationProposal] = None
    approval: Optional[ApprovalDecision] = None
    remediation: Optional[RemediationResult] = None
    postmortem: Optional[Postmortem] = None

    class Config:
        use_enum_values = True