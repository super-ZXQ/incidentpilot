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
    github = github or default_github_integration(settings)

    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import select

        from incidentpilot.models.db import AgentRun, Approval, ExternalSideEffect
        from incidentpilot.services.artifacts import binding, validated_artifact

        result = await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        service = RunService(session)
        approval = await session.scalar(select(Approval).where(Approval.run_pk == run.id))
        if approval is None or approval.decision != decision.value:
            raise ValueError("persisted human approval required")
        artifact = await validated_artifact(session, run, approval.patch_artifact_id)
        if approval.payload != binding(artifact):
            raise ValueError("approved artifact binding changed")

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

        idempotency_key = f"github_pr:{run.run_id}:{artifact.patch_hash}"
        effect_result = await session.execute(
            select(ExternalSideEffect)
            .where(ExternalSideEffect.idempotency_key == idempotency_key)
            .with_for_update()
        )
        effect = effect_result.scalar_one_or_none()
        from incidentpilot.persistence.ownership import fence

        await fence(session, run_id)
        if effect is not None and effect.status == "COMPLETED":
            from incidentpilot.observability.metrics import DUPLICATE_SIDE_EFFECTS

            DUPLICATE_SIDE_EFFECTS.inc()
            pull_request = dict(effect.result)
            await service.mark_status(
                run,
                AgentRunStatus.RESOLVED,
                workflow_state=WorkflowState.RESOLVED.value,
                result={**(run.result or {}), "pull_request": pull_request},
                finished=True,
            )
            return {"status": run.status, "pull_request": pull_request}
        if effect is None:
            effect = ExternalSideEffect(
                run_pk=run.id,
                effect_type="github_pull_request",
                idempotency_key=idempotency_key,
                status="IN_PROGRESS",
                attempt_count=1,
            )
            session.add(effect)
        else:
            effect.status = "IN_PROGRESS"
            effect.attempt_count += 1
        await session.commit()

        from incidentpilot.persistence.ownership import fence

        await fence(session, run_id)
        await session.commit()
        async def authorize():
            async with factory() as auth_session:
                await fence(auth_session, run_id)
                await auth_session.commit()

        pr = await github.acreate_pull_request(
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
            idempotency_key=idempotency_key,
            authorize=authorize,
        )
        await fence(session, run_id)
        if not pr.ok:
            effect.status = "FAILED"
            effect.error = pr.error or "github pr failed"
            await session.commit()
            if pr.details.get("retryable"):
                raise ConnectionError("transient GitHub failure; remote reconciliation required")
            await service.mark_status(
                run,
                AgentRunStatus.FAILED,
                workflow_state=WorkflowState.FAILED.value,
                error=pr.error or "github pr failed",
                finished=True,
            )
            return {"status": run.status, "error": pr.error, "mode": pr.mode}

        pull_request = {
            "url": pr.pr_url,
            "number": pr.pr_number,
            "branch": pr.branch,
            "mode": pr.mode,
        }
        effect.status = "COMPLETED"
        effect.result = pull_request
        effect.error = None
        await session.commit()
        await service.mark_status(
            run,
            AgentRunStatus.RESOLVED,
            workflow_state=WorkflowState.RESOLVED.value,
            result={
                **(run.result or {}),
                "pull_request": pull_request,
            },
            finished=True,
        )
        return {
            "status": run.status,
            "pull_request": pr.details | pull_request,
        }
