"""GitHub package."""

from incidentpilot.github.integration import (
    GitHubIntegration,
    PullRequestResult,
    default_github_integration,
)

__all__ = ["GitHubIntegration", "PullRequestResult", "default_github_integration"]
