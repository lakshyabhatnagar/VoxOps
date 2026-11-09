# Run Stage 3 locally

Use Python 3.11+, Git, uv, and a working local Docker daemon with internet access. The implementation
uses Docker with an owned rootless BuildKit container. A rootless Docker daemon and Buildx plugin
are not required. Docker must support the configured limits.

```sh
uv sync --locked
docker info
uv run voxops
```

Open `http://127.0.0.1:8000/`. Configure `VOXOPS_ALLOWED_ROOTS` as a JSON list containing your
target repository and keep `VOXOPS_DATA_DIR` outside it. Settings do not load `.env` automatically;
use `uv run --env-file .env voxops` when appropriate. See [integration testing](integration-testing.md)
for existing model and GitHub configuration.

| Method/path under `/api/v1/artifact-runs` | Action |
| --- | --- |
| `POST /plan`, `POST` to the base path | Plan from a path and strategies keyed by detected unit ID; start with repository owner/name. |
| `GET /{id}/review`, `POST /{id}/approvals` | Inspect and decide on the exact review ID/digest. |
| `POST /{id}/execute` | Generate, verify, or resume the run. |
| `GET /{id}/verification/env-form` | Read the aggregated form. |
| `POST /{id}/verification/env-values` | Submit its digest and field-ID/value map. |
| `GET /{id}/verification` | Inspect value-free status and evidence. |
| `POST /{id}/verification/cancel` | Cancel active verification. |
| `POST /{id}/publish`, `GET /{id}/result` | Publish the final approved branch and inspect its verified SHA. |

Approve any initial baseline/source review separately. Execute, fill missing values if requested,
submit once, and choose **Run or resume verification**. Inspect the final review after verification.

```json
{"review_id":"<current ID>","digest":"<current digest>","decision":"approve"}
```

For incomplete verification, additionally submit `incomplete_acknowledgement` containing
`review_id`, `review_digest`, `attestation_digest`, the exact `missing_check_ids`, and
`acknowledged: true`. Failed builds, missing mandatory inputs, and unknown runtime errors cannot
use that waiver. The JSON API provides approval/publication; the minimal tester displays the review.

Disposable demos need no model keys or GitHub access:

```sh
# Simulated Docker; real service, approvals, and local Git:
uv run python scripts/demo_stage2.py
# Real Docker, loopback HTTP, repair, and local Git; simulated model/GitHub:
uv run python scripts/demo_stage3.py
```

The real demo prints its branch/SHA and removes its temporary repository. It downloads images and
packages and intentionally exercises one COPY failure before repair. It never pushes VoxOps itself.

For `path_forbidden`, correct allowed roots and restart. For Docker permission, capability, resource,
or timeout errors, correct the reported prerequisite and execute again. After restart, resume and
re-enter lost required values. Unknown application/settings failures remain explicit errors.
