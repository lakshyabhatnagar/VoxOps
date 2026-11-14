# Stage 3 local verification

Use the [Stage 4 workflow API and frontend](../README.md#run-a-github-workflow).
Stage 3 remains its internal Docker/environment/repair implementation; its old
execute/publish endpoints and standalone CLI executor have been removed.

A working Docker daemon with internet access and MongoDB are required. The owned
rootless BuildKit container needs standard Docker, not a rootless host daemon.
Test servers bind only through loopback host ports and are removed after checks.

Environment form endpoints remain at `/api/v1/artifact-runs/{id}/verification/env-form`
and `.../env-values`. GET `.../verification` retrieves detailed checks, attempts,
redacted diagnostics and resource cleanup records. Values are held only in memory.

The supervisor separately invokes candidate generation, one verification pass, and
an authorized repair. The existing runner handles dependency ordering, parallel
independent units, toolchain/input matching and reuse. Local readiness proves only
the recorded startup/HTTP/static checks. It does not prove full application behavior.

The current workflow requires all checks to pass before final approval. See
[Stage 4 verification](stage4-verification.md) for actual real and simulated evidence.
