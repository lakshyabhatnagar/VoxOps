from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from voxops.core.errors import AppError
from voxops.domain.artifact_models import (
    ApprovalRecord,
    ArtifactRun,
    ChangeReview,
    ReviewedFile,
    ValidationResult,
)
from voxops.storage.artifact_store import digest

POLICY_FILES = (
    "artifacts/review.py",
    "artifacts/security.py",
    "artifacts/validators.py",
    "artifacts/planner.py",
    "artifacts/service.py",
    "domain/artifact_models.py",
    "repository/git_executor.py",
    "repository/snapshot.py",
    "github/publisher.py",
    "github/connection.py",
)
POLICY_CODE = digest(
    {
        path: digest((Path(__file__).resolve().parents[1] / path).read_bytes())
        for path in POLICY_FILES
    }
)


# Makes a checksum for the code and settings used to check and publish files.
# Changes to those checks or the configured GitHub endpoint change the checksum.
def policy_fingerprint(settings) -> str:
    return digest(
        {
            "code": POLICY_CODE,
            "max_file_bytes": settings.max_file_bytes,
            "optional_tools": settings.artifact_optional_tools,
            "github_mcp_url": settings.github_mcp_url,
        }
    )


# Makes a checksum of the review details and displayed changes.
# Leaves out the checksum field itself so the result can be checked later.
def review_digest(review: ChangeReview) -> str:
    return digest(review.model_dump(mode="json", exclude={"digest"}))


# Creates a two-hour review for the selected files and their displayed changes.
# Records the source, destination, checks, and actions, then saves it on the run.
def build_review(
    run: ArtifactRun,
    purpose: str,
    files: list[ReviewedFile],
    diff: str,
    parent_sha: str | None,
    validations: list[ValidationResult] | None = None,
    excluded_paths: list[str] | None = None,
) -> ChangeReview:
    if len({file.path for file in files}) != len(files):
        raise AppError("review_conflict", "Review contains duplicate file destinations.", 409)
    review = ChangeReview(
        review_id=uuid4().hex,
        run_id=run.run_id,
        purpose=purpose,
        plan_revision=run.plan.revision,
        plan_fingerprint=digest(run.plan.model_dump(mode="json")),
        policy_fingerprint=run.policy_fingerprint,
        source_fingerprint=run.source_fingerprint,
        source_head_sha=run.source_head_sha,
        source_branch=run.source_branch,
        parent_sha=parent_sha,
        target=run.target.model_copy(deep=True),
        branch=run.branch,
        files=sorted(files, key=lambda item: item.path),
        diff=diff,
        validations=validations or [],
        excluded_paths=sorted(excluded_paths or []),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        ci_may_run=any(file.path.startswith(".github/workflows/") for file in files),
        expected_actions=(
            ["Create private repository if absent", "Publish approved baseline"]
            if purpose == "baseline_publication"
            else ["Copy selected source changes into isolated worktree"]
            if purpose == "source_snapshot"
            else ["Commit approved files", "Push VoxOps branch", "Verify remote SHA"]
        ),
    )
    review.digest = review_digest(review)
    run.reviews[review.review_id] = review
    return review


# Compares the reviewed repository with the run's current repository details.
# Allows a missing ID or URL to be filled in while preserving the reviewed identity.
def target_matches(review: ChangeReview, run: ArtifactRun) -> bool:
    old, new = review.target, run.target
    return (
        old.owner == new.owner
        and old.name == new.name
        and old.private == new.private
        and (old.repository_id is None or old.repository_id == new.repository_id)
        and (old.clone_url is None or old.clone_url == new.clone_url)
        and (not old.can_push or new.can_push)
    )


# Checks that a review is unexpired and still matches the current run.
# Raises an error if its checksum, plan, source, branch, or destination has changed.
def validate_review(run: ArtifactRun, review: ChangeReview) -> None:
    if (
        review.digest != review_digest(review)
        or review.expires_at <= datetime.now(UTC)
        or review.run_id != run.run_id
        or review.plan_revision != run.plan.revision
        or review.plan_fingerprint != digest(run.plan.model_dump(mode="json"))
        or review.policy_fingerprint != run.policy_fingerprint
        or review.source_fingerprint != run.source_fingerprint
        or review.source_head_sha != run.source_head_sha
        or review.source_branch != run.source_branch
        or review.branch != run.branch
        or not target_matches(review, run)
    ):
        raise AppError("stale_review", "Review changed or expired; request a new review.", 409)


# Records an approval or rejection for the specified review and checksum.
# Rejects changed reviews and conflicting decisions, and updates the run status.
def approve_review(run: ArtifactRun, review_id: str, digest: str, decision: str) -> ApprovalRecord:
    review = run.reviews.get(review_id)
    if review is None:
        raise AppError("review_not_found", "Unknown change review.", 404)
    validate_review(run, review)
    if digest != review.digest:
        raise AppError("stale_review", "Approval digest does not match this review.", 409)
    previous = run.approvals.get(review_id)
    if previous:
        if previous.digest != digest or previous.decision != decision:
            raise AppError(
                "approval_conflict", "This review already has a different decision.", 409
            )
        return previous
    if run.status in {"rejected", "cancelled"}:
        raise AppError("run_closed", "This run is closed; create a new plan and run.", 409)
    approval = ApprovalRecord(
        approval_id=uuid4().hex,
        review_id=review_id,
        run_id=run.run_id,
        digest=digest,
        decision=decision,
    )
    run.approvals[review_id] = approval
    if decision == "reject":
        run.status = "rejected"
    elif review.purpose == "artifact_publication":
        run.status = "approved"
    return approval


# Finds the newest review for the requested purpose and checks its approval.
# Returns the review only when its matching approval is still valid.
def require_approval(run: ArtifactRun, purpose: str) -> ChangeReview:
    if run.status in {"rejected", "cancelled"}:
        raise AppError("approval_rejected", "This run has been rejected or cancelled.", 409)
    reviews = [review for review in run.reviews.values() if review.purpose == purpose]
    if not reviews:
        raise AppError("approval_required", "Review and explicit approval are required.", 409)
    review = max(reviews, key=lambda item: item.created_at)
    validate_review(run, review)
    approval = run.approvals.get(review.review_id)
    if (
        approval is None
        or approval.decision != "approve"
        or approval.digest != review.digest
        or approval.run_id != run.run_id
        or approval.review_id != review.review_id
    ):
        raise AppError("approval_required", "Review and explicit approval are required.", 409)
    return review
