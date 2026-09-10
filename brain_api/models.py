"""
Pydantic models for Alfred Brain API
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class AlfredStatus(str, Enum):
    """Alfred's current state."""

    IDLE = "idle"
    THINKING = "thinking"
    EXECUTING = "executing"
    SPEAKING = "speaking"
    ERROR = "error"


class ChatMessage(BaseModel):
    """User message to Alfred."""

    message: str = Field(..., description="The user's message")
    session_id: Optional[str] = Field(None, description="Session ID for context")
    mode: Optional[str] = Field("FOUNDER", description="Operational mode")
    approved_actions: Optional[List[str]] = Field(
        None,
        description=(
            "Action signatures the user has just approved, echoed back verbatim "
            "from a prior response's awaiting_approval.signature. Approval is "
            "per exact tool+params — a different call to the same tool still "
            "needs its own approval."
        ),
    )


class ChatResponse(BaseModel):
    """Alfred's response."""

    response: str = Field(..., description="Alfred's text response")
    status: AlfredStatus = Field(AlfredStatus.IDLE, description="Current status")
    session_id: Optional[str] = Field(None, description="Session ID")
    thinking: List[str] = Field(
        default_factory=list, description="Thinking trace - step by step reasoning"
    )
    tools_called: List[str] = Field(
        default_factory=list, description="Tools invoked during execution"
    )
    tool_results: List[Dict[str, Any]] = Field(
        default_factory=list, description="Results from tool executions"
    )
    episodes_saved: int = Field(0, description="Number of episodic memories (T3) created")
    episode_path: Optional[str] = Field(
        None, description="Path to the T3 episode file, if one was saved"
    )
    skill_used: bool = Field(False, description="Whether a matched skill guided this response")
    skill_generated: bool = Field(
        False, description="Whether a new skill was generated from this task"
    )
    awaiting_approval: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Present when a tool call was blocked pending approval. Contains "
            "tool/params/signature; resend the request with the signature in "
            "approved_actions to proceed."
        ),
    )


class StatusResponse(BaseModel):
    """Alfred's current status."""

    status: AlfredStatus
    current_task: Optional[str] = None
    phase: Optional[str] = None
    uptime_seconds: int = 0
    memory_stats: Dict[str, Any] = Field(default_factory=dict)
    skills_count: int = 0


class ContextResponse(BaseModel):
    """Context data for UI panels."""

    current_task: Optional[str] = None
    recent_messages: List[Dict[str, str]] = Field(default_factory=list)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    calendar: List[Dict[str, Any]] = Field(default_factory=list)
    weather: Optional[Dict[str, Any]] = Field(None)
    skills_available: List[str] = Field(default_factory=list)


class SkillInfo(BaseModel):
    """Skill information."""

    id: str
    title: str
    complexity: str
    success_rate: float = 0.0
    tags: List[str] = Field(default_factory=list)


class SkillsResponse(BaseModel):
    """List of available skills."""

    skills: List[SkillInfo]
    total: int


class TaskInfo(BaseModel):
    """Task information."""

    id: str
    title: str
    completed: bool = False
    priority: str = "medium"
    due_date: Optional[str] = None


class TasksResponse(BaseModel):
    """List of tasks."""

    tasks: List[TaskInfo]
    total: int


class VoiceInput(BaseModel):
    """Voice input data."""

    audio: str = Field(..., description="Base64 encoded audio")
    format: str = Field("wav", description="Audio format")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = "3.0.0"
    uptime_seconds: int = 0
    memory_tiers: Dict[str, int] = Field(default_factory=dict)
