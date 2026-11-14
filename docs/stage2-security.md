# Stage 2 security boundaries

Historical Stage 1–3 evidence below describes the original implementation. Stage 4
replaces operational JSON files and filesystem locks with MongoDB records and a
single-process execution boundary. See [Stage 4 decisions](stage4-decisions.md).

Run VoxOps as a trusted local service. It is not a public multi-user server or a sandbox. Keep its data directory outside the target repository. Limit `VOXOPS_ALLOWED_ROOTS` to directories the operator trusts.

Artifact paths are canonical repository-relative paths. Reads and writes reject traversal and symlink components. Source inventory is conservative and excludes hidden paths apart from supported exceptions. Review excluded paths before approving a baseline or snapshot. Secret detection is a bounded pattern check, not a guarantee that every secret is found. Text is checked before model input, staging, and publication. Binary assets are allowed only at trusted Git boundaries and are still scanned for recognizable secrets.

Workers cannot choose their own output paths or write the shared worktree. The coordinator checks task ownership, hashes, and staged content before applying changes. Reviews bind the target, branch, source revision, file hashes, modes, diff, and expiration to a digest. A rejection or changed source invalidates the corresponding authorization. Review the disclosed CI actions before approval because a push can trigger CI.

Git publication permits canonical `https://github.com/OWNER/NAME.git` URLs only. SSH and arbitrary Git transports are unsupported. Git ignores project credential helpers and other unsafe project configuration. An operator may explicitly configure a trusted absolute credential helper through `VOXOPS_GIT_CREDENTIAL_HELPER`. Keep credentials out of URLs, source files, and logs. The MCP endpoint requires HTTPS except for loopback HTTP. MCP authentication and Git authentication are separate.

Workflow validation restricts permissions, actions, shell expressions, and network tools. These checks do not replace a review of the generated workflow. See [GitHub Actions security hardening](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions).

## Untrusted input and stored evidence

Repository text, package scripts, README instructions and validator feedback are data. PydanticAI specialists have no Git, MCP or shell tools. A typed output must name the exact planned path. The collector reads through no-follow directory descriptors, caps text, and omits secret-bearing files. It never reads .env values, npm auth, private keys or Git internals into model context. Compound credential names, recognizable tokens and credentials in URLs/headers are rejected or redacted. No raw model transcripts are retained.

Proposals use private content-addressed blobs. Attempt ledgers record digests, bounded diagnostics and usage. An unfinished request retains its persisted token reservation after a crash. The single coordinator verifies original hashes before writing the owned worktree. A changed source, plan, validation-code/configuration policy, target, parent or file invalidates approval. Versioned JSON is private and atomically replaced. It is not a hosted database or a durable graph checkpointer.

Git reads exact blobs and executable modes. Baseline and artifact commits use an exact private index, so unrelated staged files cannot leak into a commit. The publisher scans tracked history and the candidate index, including secrets that .gitignore cannot hide. Hooks, filters, signing, fsmonitor, submodule recursion, transport rewrites and project credential helpers cannot supply executable authority. Only an explicitly configured trusted Git helper can authenticate HTTPS.

Source selection for an initial baseline is explicit. The inventory discloses excluded file names and directory markers without reading their values. Unsupported tracked hidden paths, symlinks and submodules block publication. Do not treat an exclusion list as permission to publish a secret. Resolve a probable-secret false positive by removing/sanitizing the literal and reviewing the source/history again. There is no bypass approval flag.

## CI and local service

CI actions must be approved official actions pinned to full SHAs. Permissions are read-only. Checkout must set persist-credentials: false. Secret and GitHub-token expressions, unsafe shell interpolation, deployment commands and obvious network/cloud tools are blocked. A source build script still executes code on the remote runner after push. Review that code and the disclosed triggers. Static validation cannot prove arbitrary build scripts harmless or prove that an action SHA exists.

The FastAPI peer, Host and Origin middleware protects all Stage 2 routes. There is no public deployment or remote authentication layer in this stage. Loopback access is trusted local access. A Git worktree is isolation for files and branches, not a process sandbox. No project npm/pip/build script or Docker build is run locally during generation. Optional actionlint uses a bounded parser invocation with external shellcheck/pyflakes execution disabled.

Stage 3 adds isolated execution. Stage 4 adds production orchestration durability. Stage 5 adds provider deployment. Keep cloud credentials and deployment outside this feature.
