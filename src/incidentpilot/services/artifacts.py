"""Validate the exact immutable patch and its successful test record."""

from sqlalchemy import select

from incidentpilot.models.db import PatchArtifact, TestRun
from incidentpilot.sandbox.manager import hash_patch


async def validated_artifact(session, run, artifact_id: str | None = None):
    artifact_id = artifact_id or (run.result or {}).get("patch_artifact_id")
    artifact = await session.scalar(select(PatchArtifact).where(
        PatchArtifact.run_pk == run.id,
        PatchArtifact.patch_artifact_id == artifact_id,
    ))
    if artifact is None or not artifact.immutable:
        raise ValueError("immutable artifact missing or belongs to another run")
    if hash_patch(artifact.patch_diff) != artifact.patch_hash:
        raise ValueError("artifact patch hash mismatch")
    test = await session.scalar(select(TestRun).where(
        TestRun.run_pk == run.id, TestRun.test_run_id == artifact.test_run_id,
    ))
    if (test is None or test.status != "PASSED" or test.exit_code != 0
            or test.patch_hash != artifact.patch_hash
            or test.base_commit_sha != artifact.base_commit_sha):
        raise ValueError("artifact has no matching successful test")
    return artifact


def binding(artifact) -> dict[str, str]:
    return {key: getattr(artifact, key) for key in (
        "patch_artifact_id", "patch_hash", "base_commit_sha", "test_run_id",
    )}
