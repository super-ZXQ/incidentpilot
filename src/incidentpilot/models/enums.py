"""Shared domain enums and constants."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    INVESTIGATING = "INVESTIGATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_HUMAN_INTERVENTION = "NEEDS_HUMAN_INTERVENTION"
    FAILED = "FAILED"


class WorkflowState(StrEnum):
    INCIDENT_RECEIVED = "INCIDENT_RECEIVED"
    PLAN = "PLAN"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    FORM_HYPOTHESIS = "FORM_HYPOTHESIS"
    VERIFY_HYPOTHESIS = "VERIFY_HYPOTHESIS"
    ROOT_CAUSE_FOUND = "ROOT_CAUSE_FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CREATE_SANDBOX = "CREATE_SANDBOX"
    GENERATE_PATCH = "GENERATE_PATCH"
    RUN_TESTS = "RUN_TESTS"
    REFLECT = "REFLECT"
    EXPORT_PATCH_ARTIFACT = "EXPORT_PATCH_ARTIFACT"
    DESTROY_SANDBOX = "DESTROY_SANDBOX"
    WAIT_FOR_APPROVAL = "WAIT_FOR_APPROVAL"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    RESOLVED = "RESOLVED"
    NEEDS_HUMAN_INTERVENTION = "NEEDS_HUMAN_INTERVENTION"
    FAILED = "FAILED"


class EvidenceSourceType(StrEnum):
    METRICS = "metrics"
    LOGS = "logs"
    GIT_HISTORY = "git_history"
    GIT_DIFF = "git_diff"
    SOURCE_CODE = "source_code"
    DATABASE = "database"
    TEST_RESULT = "test_result"
    OTHER = "other"


class ToolCallStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    DENIED = "DENIED"


class PatchAttemptStatus(StrEnum):
    CREATED = "CREATED"
    TESTS_PASSED = "TESTS_PASSED"
    TESTS_FAILED = "TESTS_FAILED"
    EXPORTED = "EXPORTED"
    DISCARDED = "DISCARDED"


class TestRunStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class AgentRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    INVESTIGATION_COMPLETE = "INVESTIGATION_COMPLETE"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_HUMAN_INTERVENTION = "NEEDS_HUMAN_INTERVENTION"
    FAILED = "FAILED"


TERMINAL_RUN_STATUSES = {
    AgentRunStatus.INVESTIGATION_COMPLETE,
    AgentRunStatus.RESOLVED,
    AgentRunStatus.INSUFFICIENT_EVIDENCE,
    AgentRunStatus.NEEDS_HUMAN_INTERVENTION,
    AgentRunStatus.FAILED,
}

# Read-only external tools (MCP boundary)
READONLY_TOOL_NAMES = (
    "read_metrics",
    "read_logs",
    "inspect_git_history",
    "inspect_git_diff",
    "read_source_code",
    "query_database_readonly",
)

# Sandboxed mutation tools
SANDBOX_TOOL_NAMES = (
    "create_sandbox",
    "apply_patch",
    "run_tests",
    "inspect_test_results",
    "inspect_patch_diff",
)

# Human approval required
APPROVAL_TOOL_NAMES = ("create_pull_request",)

FORBIDDEN_SQL_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "grant",
    "revoke",
)
