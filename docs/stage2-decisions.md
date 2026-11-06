# Stage 2 decisions

## Artifact planning

Each detected unit needs an explicit `container`, `static`, or `native` strategy. The planner rejects combinations that conflict with detected runtime. It creates Docker tasks only for container units, with one root `.dockerignore` and one repository CI task. Existing files are validated before repair. A valid existing artifact keeps its content and hash. Source changes require an explicit path, expected hash, reason, and correction.

An incomplete scan or unresolved runtime question blocks starting the run. Unsupported providers add an unresolved question rather than inventing a provider schema. Stage 3 adds isolated build/runtime execution. Stage 4 adds a durable supervisor. Stage 5 adds provider ranking and deployment. Stage 6 covers the later product interface and account features.

## Generation and validation

A task has at most three content attempts, subject to shared request and token budgets. Validation is static and checks artifact-specific rules and cross-artifact consistency. It does not prove that an application builds or runs. Docker build checks are reported unavailable by the current implementation. Optional `actionlint` runs only when enabled and installed. See [Docker build checks](https://docs.docker.com/build/checks/) and [actionlint](https://github.com/rhysd/actionlint) for the tools' own scope.

Model credentials are optional for planning and validation of existing artifacts. Missing credentials leave required generation blocked. They do not produce placeholder artifacts.

## Source and publication

The source repository is preserved. A new private repository requires a baseline review. For existing committed history, the baseline contains all tracked files, rather than a selected subset of history. For a repository without a commit, the baseline contains the explicitly selected safe files. Dirty source changes require an explicit snapshot selection and review, including omitted paths. Publication uses a separate branch and requires artifact review and approval.

GitHub MCP tool names and schemas are discovered at runtime. Repository search supplies ID, full name, and visibility. Legacy responses may include push permission. Current curated responses use the discovered `list_repository_collaborators` tool to verify the authenticated user's `admin`, `maintain`, or `write` role. Unknown roles, missing capabilities, incomplete results and ambiguous metadata block publication. This follows the current [GitHub MCP search](https://github.com/github/github-mcp-server/blob/main/pkg/github/search.go) and [collaborator tool](https://github.com/github/github-mcp-server/blob/main/pkg/github/repositories.go). A new repository is private. An existing public repository is disclosed before publication.

## Recovery and limits

PydanticAI and the provider SDK have automatic content/transport retries disabled. Each application attempt reserves tokens before dispatch. An interrupted or unmetered attempt consumes its full persisted reservation on resume. Recorded provider usage is otherwise charged against the run and task budgets. Provider usage limits can detect an overrun after a response; they are not a billing guarantee. [PydanticAI usage limits](https://pydantic.dev/docs/ai/core-concepts/retries/) describe that boundary.

The atomic JSON store recovers domain-operation boundaries, including completed tasks, MCP creation intent, baseline and artifact commit identities, and remote receipts. It does not checkpoint arbitrary LangGraph nodes. A project filesystem lock excludes concurrent mutations. Re-running execution preserves completed siblings. Retrying publication reconciles the same commit and remote branch. A rejected run is closed; create a new plan/run to revise it.

The supported Git path uses standalone repositories, canonical GitHub HTTPS remotes and user-managed authentication. Linked source worktrees, shallow history, alternates, grafts, symlinks, submodules and unsupported hidden tracked paths are blocked. History scanning is limited to 1,000 commits and files to the configured byte bound. Explicit file selection controls a new baseline; VoxOps does not interpret arbitrary .gitignore semantics for that selection. Reviews disclose excluded path names, never their values.

Normal pushes never request force. Observed remote collisions are rejected and the final SHA is rechecked. This does not implement an atomic create-only lease across a competing actor's pre-advertisement branch creation. Unique run branches and Git's normal fast-forward rules limit that race without claiming a distributed lock.

## Engineering workflow

The installed pstack 0.9.129 skills were used through their Codex mapping. Two read-only design investigations compared alternatives before implementation. Validator/context and Git/MCP code slices used disjoint temporary checkouts. Independent reviewers ran actual repros. No global model-role configuration or project push/merge was performed during Stage 2 implementation. The locally declared model substitutions and decision trail are kept under `.audit/` as local-only records excluded from Git. The user later replaced the original Why/How comment format with two plain-English explanation lines above each function and class. These explain what the code does and how its information is used, without requiring the old labels.

Model the Domain shaped typed task ownership and digest-bound review records. Prove It Works shaped the actual Git and HTTP demonstrations. The decisions extend Stage 1's WorktreeManager and private atomic store instead of replacing them.
