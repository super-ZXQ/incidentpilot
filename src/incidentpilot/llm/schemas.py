"""Structured LLM output schemas for investigation decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolSelection(BaseModel):
    tool_name: str
    reason: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class InvestigationPlan(BaseModel):
    steps: list[str] = Field(default_factory=list)
    focus: str = ""


class HypothesisProposal(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


class VerificationDecision(BaseModel):
    hypothesis_supported: bool
    notes: str = ""
    additional_tools: list[str] = Field(default_factory=list)


class RootCauseConclusion(BaseModel):
    statement: str
    fault_category: str = ""
    affected_component: str = ""
    causal_facts: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


class PatchProposal(BaseModel):
    patch_type: Literal["simple_replace", "unified_diff"] = "simple_replace"
    path: str = ""
    find: str = ""
    replace: str = ""
    notes: str = ""


class NextAction(BaseModel):
    """LLM decides the next investigation step."""

    action: Literal[
        "collect_evidence",
        "form_hypothesis",
        "verify_hypothesis",
        "declare_root_cause",
        "insufficient_evidence",
        "needs_human",
    ]
    tool_selection: ToolSelection | None = None
    hypothesis: HypothesisProposal | None = None
    root_cause: RootCauseConclusion | None = None
    reason: str = ""
