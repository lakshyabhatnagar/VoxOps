# Plan and publish artifacts

This describes the original Stage 2 workflow. The current V1 path adds mandatory local verification
before final artifact approval and publication. See [Stage 3 usage](stage3-usage.md).

Stage 2 uses a trusted local server. Install dependencies and set paths before starting it. The data directory must be outside the target repository, and every allowed root must already exist.

```sh
uv sync --locked
export VOXOPS_ALLOWED_ROOTS='["/absolute/path/to/workspaces"]'
export VOXOPS_DATA_DIR='/absolute/path/to/voxops-data'
uv run voxops
```

Planning and existing-artifact validation do not require a model key. To generate missing artifacts, configure `VOXOPS_MODEL` and the matching `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Missing credentials block generation rather than creating fake files. Model calls may incur provider charges.

Publication requires a configured GitHub MCP server and separately configured Git HTTPS authentication. Set `VOXOPS_GITHUB_MCP_URL` and, when needed, `VOXOPS_GITHUB_MCP_TOKEN`. The MCP endpoint must use HTTPS, except that loopback HTTP is allowed. If Git needs a credential helper, set `VOXOPS_GIT_CREDENTIAL_HELPER` to an explicitly trusted absolute helper path. Project-local helpers are not trusted. Git publication supports canonical GitHub HTTPS URLs only, not SSH.

The existing browser API tester covers Stage 1. Use the following API sequence for Stage 2. Substitute real values from each response. Full schemas are available at `http://127.0.0.1:8000/docs`.

1. Analyze the source and find each `detected_units[].id` and its runtime evidence.

   ```sh
   curl -s http://127.0.0.1:8000/api/v1/repositories/analyze \
     -H 'Content-Type: application/json' \
     -d '{"path":"/absolute/path/to/workspaces/project","use_llm":false}'
   ```

2. Plan with one strategy for every detected unit. Replace `UNIT_ID` with the ID returned by analysis. Valid strategies are `container`, `static`, and `native`, subject to runtime compatibility. Inspect `tasks`, `warnings`, and `unresolved_questions`. Resolve any questions before starting.

   ```sh
   curl -s http://127.0.0.1:8000/api/v1/artifact-runs/plan \
     -H 'Content-Type: application/json' \
     -d '{"path":"/absolute/path/to/workspaces/project","strategies":{"UNIT_ID":"container"}}'
   ```

3. Start the saved plan with its `plan_id` and intended GitHub owner and repository name. For a source with no commit, supply `selected_paths` with the safe files for the baseline. Review the returned status and errors.

   ```sh
   curl -s http://127.0.0.1:8000/api/v1/artifact-runs \
     -H 'Content-Type: application/json' \
     -d '{"plan_id":"PLAN_ID","owner":"OWNER","name":"REPOSITORY","selected_paths":["main.py","requirements.txt"]}'
   ```

4. If the run requires source selection, submit the exact paths. An empty list deliberately omits all changes. Existing committed history for a new repository publishes all tracked history; it cannot be reduced by selecting a subset. For dirty source, select only the changes to include.

   ```sh
   curl -s -X POST http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/source-selection \
     -H 'Content-Type: application/json' -d '{"paths":["main.py"]}'
   ```

5. If a baseline or snapshot review is required, inspect it. Check the target, files, omissions, diff, and disclosed actions. Approve only the exact displayed `review_id` and `digest`, or send `reject`.

   ```sh
   curl -s http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/review
   curl -s -X POST http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/approvals \
     -H 'Content-Type: application/json' \
     -d '{"review_id":"REVIEW_ID","digest":"REVIEW_DIGEST","decision":"approve"}'
   ```

6. Execute the run. If it requests another source review, inspect and approve that review before continuing. Inspect task statuses and validation evidence. If generation is blocked or validation fails, resolve the reported cause before proceeding.

   ```sh
   curl -s -X POST http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/execute
   curl -s http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID
   ```

7. Inspect the artifact review with the review endpoint. Approve its own `review_id` and `digest` using the approval endpoint above. Approval of a baseline or snapshot does not approve the artifacts.

8. Publish, then inspect the result. The result identifies the verified branch and commit and reports `deployment_executed: false`.

   ```sh
   curl -s -X POST http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/publish
   curl -s http://127.0.0.1:8000/api/v1/artifact-runs/RUN_ID/result
   ```

The offline demo copies a checked-in fixture and injects fake model and GitHub adapters. Actual Git commits and pushes go only to a newly created local bare repository. Its explicit offline client supplies approvals to exercise the API. Production service routes never approve automatically.

```sh
uv run python scripts/demo_stage2.py --offline --fixture fastapi_only
uv run python scripts/demo_stage2.py --offline --bootstrap
uv run python scripts/demo_stage2.py --offline --reject
uv run python scripts/demo_stage2.py --offline --fixture vite_only
uv run python scripts/demo_stage2.py --help
```

Use `--keep /absolute/path/to/new-directory` to retain the source, private records, owned worktree and bare remote for inspection. The directory must not exist. The Vite static case generates no Dockerfile. These fixtures demonstrate orchestration and Git semantics; their CI and images are not built.

## Configuration and recovery

Enable the MCP `get_me`, `search_repositories`, and `create_repository` tools when creating a repository. Current curated search metadata also needs `list_repository_collaborators`. The authenticated user must have a verified write role. A server that cannot supply that evidence returns an actionable configuration error. Configure the runtime MCP service separately from any coding assistant connector.

| Setting | Default and limit |
| --- | --- |
| `VOXOPS_ARTIFACT_CONCURRENCY` | 3 workers, range 1 to 8. |
| `VOXOPS_ARTIFACT_MAX_REQUESTS` | 60 content requests per run, maximum 300. |
| `VOXOPS_ARTIFACT_MAX_TOKENS` | 150,000 reported or conservatively reserved tokens per run. |
| `VOXOPS_ARTIFACT_TASK_TOKENS` | 20,000 tokens per artifact across its attempts. |
| `VOXOPS_ARTIFACT_STAGED_BYTES` | 8 MiB total private staged content. |
| `VOXOPS_ARTIFACT_OPTIONAL_TOOLS` | False. Enables the installed actionlint parser only when true. |
| `VOXOPS_MAX_FILE_BYTES` | 128 KiB per source file by default; generated text is capped at 128 KiB. |
| `VOXOPS_MAX_CONTEXT_CHARS` | 16,000 characters per structured context packet. |
| `VOXOPS_MODEL_TIMEOUT` | 20 seconds per content attempt. |
| `VOXOPS_GIT_TIMEOUT` | 30 seconds per Git command. |
| `VOXOPS_GIT_AUTHOR_NAME`, `VOXOPS_GIT_AUTHOR_EMAIL` | VoxOps commit identity, configurable by the operator. |

Settings are read at startup. After configuring a model, restart with the same data directory and retry `execute`. Completed siblings are reused. Missing MCP setup can be re-probed with `source-selection` before worktree preparation. Retry `publish` after a partial commit/push failure; the stored commit identity is reconciled before another push. A changed source, expired review, changed plan, validation policy or MCP endpoint needs a fresh plan/run. Rejection closes the run; it cannot be revived through source selection.

Source patches are optional explicit plan inputs. Each `patches` item names `target_path`, `expected_hash`, `reason` and `correction`. Only existing supported source/config files are eligible. Lockfiles, env files, invented URLs and newly permissive CORS are blocked. The patch appears separately as `source_patch` in the final review.

Inspect an owned worktree using `GET /api/v1/worktrees/WORKTREE_ID`. Cleanup uses `DELETE /api/v1/worktrees/WORKTREE_ID`. Dirty or committed worktrees require the explicit `?discard_changes=true` policy. Inspect them first. Stage 2 never automatically deletes reviewed or published worktrees. See [verification](stage2-verification.md) and [security](stage2-security.md).
