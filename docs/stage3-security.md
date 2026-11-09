# Stage 3 security boundary

VoxOps is a trusted loopback-only local tool. Project install/build/start commands run in Docker,
never as a host fallback. Ordinary containers do not guarantee protection from malicious workloads;
the operator remains responsible for Docker permissions and the host.

Build and runtime internet access stays enabled. The driver does not mount the user's home,
Docker socket, VoxOps credentials, or broad host directories into project containers. Sanitized
frozen contexts exclude environment files, keys, Git metadata, and excluded local state.

The nonprivileged rootless BuildKit container needs unconfined seccomp/AppArmor profiles on
supported hosts. These allowances reduce builder sandbox protections. Missing capabilities fail
explicitly rather than switching to privileged or host execution.

| Settings with `VOXOPS_` prefix | Default |
| --- | --- |
| `EXECUTION_MAX_PARALLEL` | 2 units, additionally limited by host capacity. |
| `BUILD_TIMEOUT_SECONDS`, `STARTUP_TIMEOUT_SECONDS` | 600 seconds, 90 seconds. |
| `EXECUTION_BUILD_CPUS`, `EXECUTION_BUILD_MEMORY_MB`, `EXECUTION_BUILD_PIDS` | 2 CPUs, 3072 MiB, 512 processes. |
| `EXECUTION_RUNTIME_CPUS`, `EXECUTION_RUNTIME_MEMORY_MB`, `EXECUTION_RUNTIME_PIDS` | 1 CPU, 512 MiB, 128 processes. |
| `EXECUTION_LOG_MAX_BYTES`, `EXECUTION_LOG_MAX_LINES` | 65536 bytes, 500 lines. |
| `EXECUTION_PROBE_TIMEOUT_SECONDS` | 5 seconds per request. |
| `EXECUTION_NODE_IMAGE`, `EXECUTION_BUILDKIT_IMAGE` | `node:22-bookworm-slim`, `moby/buildkit:rootless`. |

Limits are inspected before execution. Probes use owned loopback ports, reject redirects, and
distinguish reachability from declared health. Logs and deadlines are bounded.

Secrets remain in memory or private transient injection files. Public identities use random
configuration-generation markers, not secret-derived hashes. Unsupported build-secret injection
fails. Known values are redacted before diagnostics reach storage or a model.

Cleanup checks recorded IDs/names and exact ownership labels; it never globally prunes Docker.
Uncertain late creates remain recoverable. Stage 2 still checks source provenance, secrets, Git
configuration, reviewed trees, approvals, and remote receipts. Stage 3 adds exact verification and
live configuration authority before commit/push. It does not prove business logic, external-service
health, production security, or cloud availability.
