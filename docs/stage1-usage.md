# Stage 1 API and development harness

Start with the README quickstart. The default workspace is the service's current directory. Paths
must exist within `VOXOPS_ALLOWED_ROOTS`; parent traversal and canonical symlink escapes are
rejected. Symlinks encountered during scanning are skipped entirely. The server is a single local
process; do not run multiple worker processes against one worktree registry.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Versioned liveness, no external calls |
| `GET /ready` | Storage and configured roots; optional providers do not gate readiness |
| `POST /api/v1/repositories/analyze` | `{path, use_llm: false, transcript?: text}` |
| `POST /api/v1/worktrees` | `{path}`; requires a committed source repository root |
| `GET /api/v1/worktrees` | Recorded owned worktree status |
| `GET /api/v1/worktrees/{id}` | Ownership-verified live metadata |
| `DELETE /api/v1/worktrees/{id}` | Remove clean worktree and generated branch |
| `DELETE /api/v1/worktrees/{id}?discard_changes=true` | Explicitly discard isolated edits |
| `POST /api/v1/voice/transcribe` | `{mode: "transcript", text, language?}` |

`mode: "gnani"` returns HTTP 503 with `gnani_not_configured`. There is no speculative audio upload
contract. OpenAPI and interactive schemas are available on the loopback server at `/docs`.
Known failures use `{"error":{"category":"...","message":"safe explanation"}}`; validation
responses do not echo input values. Analysis includes `schema_version`, scan ID/timestamp/Git state,
relative file evidence, unit runtime commands, value-free config requirements, unresolved questions
and model usage. Unit dependency links remain unverified candidates when supported by evidence;
none is inferred merely because an environment name mentions an API.

Example development transcript:

```sh
curl -s http://127.0.0.1:8000/api/v1/voice/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"mode":"transcript","text":"analyze this repository","language":"en-IN"}'
```

The harness accepts a path and optional transcript:

```sh
uv run python scripts/demo_stage1.py "$PWD/tests/fixtures/fastapi_only" \
  --transcript "analyze this repository"
```

To demonstrate worktrees use **an existing initialized, committed repository** inside an allowed
root. The scanner works on uninitialized directories; linked worktrees require HEAD.

```sh
uv run python scripts/demo_stage1.py /absolute/path/to/committed/repository --worktree
```

This creates `voxops/<generated-id>` at the verified source SHA, inspects the new worktree,
then removes it and its branch in a `finally` block. Temporary locations live outside the source,
under the operating system's temporary directory. The source branch, HEAD, tracked changes and
untracked files stay untouched. Source dirty state is allowed with a warning: local edits are
**not in the worktree**. No automatic snapshots, commits or pushes occur.

Inspect or clean up later using the recorded ID if the harness is interrupted. A changed/unknown
worktree is refused; dirty known worktrees require explicit discard consent. MongoDB ownership records contain executor-only temporary worktree/source locations; scan records contain no absolute repository path.
Keep MongoDB credentials private and configure `VOXOPS_MONGO_URI`. Old local JSON records are not imported. Worktrees may contain source repository
secrets if those secrets were already committed: Stage 1 does not copy or snapshot local `.env` files.

The API doesn't load a workspace `.env` file automatically. Export the desired settings, or use
`uv run --env-file /path/to/private/service.env voxops`. Do not confuse service credentials
with the scanned repository's configuration. Never send credentials as transcript text.
