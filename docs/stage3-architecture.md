# Stage 3 execution architecture

Stage 3 checks the unpublished Stage 2 worktree before final artifact approval.
`ArtifactService` coordinates the existing components; `GitPublisher` remains the only publisher.

```mermaid
flowchart TD
  A[Stage 2 generation and static validation] --> B[Apply proposals to owned worktree]
  B --> C[Resolve defaults and existing environment]
  C --> D[One form if required values are missing]
  D --> E[Docker build and startup or static-output check]
  E -->|Concrete owned artifact error| F[Existing Stage 2 repair worker]
  F --> B
  E -->|Passed or grounded incomplete| G[Post-repair diff and verification evidence]
  G --> H[Final approval and incomplete acknowledgement if needed]
  H --> I[Existing publisher commits and pushes approved branch]
  I --> J[Verify remote SHA and return handoff]
```

| Code | Responsibility |
| --- | --- |
| `artifacts/service.py` | Generation, execution, repair application, review, cancellation, and publication. |
| `execution/environment_analysis.py`, `environment.py` | Static discovery and volatile value handling. |
| `execution/planner.py`, `contexts.py` | Framework strategies and frozen eligible inputs. |
| `execution/runner.py`, `readiness.py` | Per-unit build, startup, HTTP, and static-output evidence. |
| `execution/docker_driver.py` | Bounded commands, resource ownership, and cleanup. |
| `execution/diagnostics.py`, `repair.py` | Failure classification and existing-task repair selection. |
| `execution/candidate.py`, `github/publisher.py` | Exact candidate identity and publication gate. |

`ExecutionRun` links plans, per-unit results, attempts, cleanup records, and an attestation.
Its states include `waiting_for_env`, `verifying`, `passed`, `verification_incomplete`, `failed`,
`blocked`, `cancelled`, and `interrupted`. Tasks follow build → startup → probe, or build → static
output. Independent units share the configured resource budget. Complete input digests determine reuse.

The existing project lock protects short state updates. An operation claim prevents competing
mutations while Docker runs outside the lock. Cancellation signals active work; recovery reconciles
recorded owned resources and interrupted attempts.

Final approval binds the post-repair diff and attestation. Before commit and push, the publisher
checks the actual reviewed tree and live configuration authority. Stage 5 receives the approved
branch, verified SHA, execution plan, and truthful status. No cloud deployment occurs here.
