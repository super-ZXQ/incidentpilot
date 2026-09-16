"""Resume workflow after Human Approval to create Pull Request."""

from __future__ import annotations

import logging
from typing import Any

from incidentpilot.config import Settings, get_settings
from incidentpilot.github.integration import GitHubIntegration, default_github_integration
from incidentpilot.models.enums import AgentRunStatus, ApprovalDecision, WorkflowState
from incidentpilot.persistence.session import get_session_factory
from incidentpilot.services.incidents import RunService

logger = logging.getLogger(__name__)


async def resume_after_approval(
    *,
    run_id: str,
    decision: ApprovalDecision,
    settings: Settings | None = None,
    github: GitHubIntegration | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    github = github or default_github_integration()

    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, PatchArtifact

        result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        service = RunService(session)

        if decision == ApprovalDecision.REJECT:
            await service.mark_status(
                run,
                AgentRunStatus.NEEDS_HUMAN_INTERVENTION,
                workflow_state=WorkflowState.NEEDS_HUMAN_INTERVENTION.value,
                error="human rejected approval",
                finished=True,
            )
            return {"status": run.status, "decision": decision.value}

        # APPROVE path
        await service.mark_status(
            run,
            AgentRunStatus.RUNNING,
            workflow_state=WorkflowState.CREATE_PULL_REQUEST.value,
        )

        art_result = await session.execute(
            select(PatchArtifact).where(PatchArtifact.run_pk == run.id)
        )
        artifact = art_result.scalars().first()
        if artifact is None:
            await service.mark_status(
                run,
                AgentRunStatus.FAILED,
                workflow_state=WorkflowState.FAILED.value,
                error="no PatchArtifact available for PR creation",
                finished=True,
            )
            return {"status": run.status, "error": "missing patch artifact"}

        pr = github.create_pull_request(
            run_id=run.run_id,
            title=f"IncidentPilot fix for {run.run_id}",
            body=(
                f"Automated fix proposal for run {run.run_id}.\n\n"
                f"Patch hash: {artifact.patch_hash}\n"
                f"Base commit: {artifact.base_commit_sha}\n"
                f"Test run: {artifact.test_run_id}\n"
            ),
            patch_diff=artifact.patch_diff,
            base_commit_sha=artifact.base_commit_sha,
        )
        if not pr.ok:
            await service.mark_status(
                run,
                AgentRunStatus.FAILED,
                workflow_state=WorkflowState.FAILED.value,
                error=pr.error or "github pr failed",
                finished=True,
            )
            return {"status": run.status, "error": pr.error, "mode": pr.mode}

        await service.mark_status(
            run,
            AgentRunStatus.RESOLVED,
            workflow_state=WorkflowState.RESOLVED.value,
            result={
                **(run.result or {}),
                "pull_request": {
                    "url": pr.pr_url,
                    "number": pr.pr_number,
                    "branch": pr.branch,
                    "mode": pr.mode,
                },
            },
            finished=True,
        )
        return {
            "status": run.status,
            "pull_request": pr.details | {"url": pr.pr_url, "mode": pr.mode, "number": pr.pr_number},
        }
