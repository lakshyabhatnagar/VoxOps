# Real-repository pipeline test and bug report

Test date: 12 November 2026, Asia/Kolkata. VoxOps branch: `stage3/sandboxed-local-execution`. Tested HEAD: `78e5d6d`, plus the pre-existing uncommitted English comments. Product code was not changed for these tests.

Nine unique public GitHub repositories were tested. Each case followed transcript input, Stage 1 analysis, worktree operations, Stage 2 planning and generation, and Stage 3 when earlier gates allowed it. None of the nine live-model cases completed the full workflow. A blocked gate remained blocked.

The test imported each pinned repository's unchanged working-file contents into a fresh temporary Git baseline. Local `.env` and excluded files remained local and were not committed. Original upstream Git history was not republished. Each test's source baseline used a disposable local bare remote. GitHub metadata was simulated at that publication boundary. The configured GitHub MCP connection was checked separately with read-only calls and exposed 49 tools.

The live model was `nvidia:mistralai/mistral-large-4-1t-a52b`. Docker was real, using engine 29.9.2 on a 4-CPU, 8-GiB host. Tests used a 60-second model timeout, 240-second build timeout, 45-second startup timeout, one artifact worker, a 1-MiB file limit, and a 32,000-character model-context limit. The increased file/context limits are existing supported settings, not code changes.

A representative demo transcript was:

> Please inspect this microsoft/python-sample-vscode-fastapi-tutorial project, work out which apps it contains, prepare the deployment files it needs, build it locally, and show me the changes before publishing a branch. Do not deploy it to a cloud.

The text went through `/api/v1/voice/transcribe` in `transcript` mode, then into repository analysis. Every transcript used `transcript_stub`. No audio or Gnani request was made. Strategies were supplied explicitly by the harness from the analysis results. Natural-language intent is not being translated into executable operations by this test.

## Results by pinned repository

| Repository | Revision | Stage 1 | Stage 2 | Stage 3 |
| --- | --- | --- | --- | --- |
| [microsoft/python-sample-vscode-fastapi-tutorial](https://github.com/microsoft/python-sample-vscode-fastapi-tutorial/tree/e84de17b833ae85f48e00e83f612066e1ec26cde) | `e84de17b833a` | FastAPI detected | Dockerfile and ignore files passed. CI timed out, then had no task token budget left. | Not reached through service. Isolated real Docker build/start/HTTP passed. |
| [mgershenzon/fastapi-example](https://github.com/mgershenzon/fastapi-example/tree/b9b7282c3a9566e2a0888e8a92ab19acf613dd65) | `b9b7282c3a95` | Packaged FastAPI detected | Context rejected at 51,573 characters, above the largest supported 32,000 setting. | Not reached. |
| [emisasal/vite-react-typescript](https://github.com/emisasal/vite-react-typescript/tree/3bbadfe25845c29016eb704cf23563ffaed27104) | `3bbadfe25845` | React/Vite detected | 187,294-byte npm lockfile rejected despite 1,048,576-byte configured file limit. | Not reached. |
| [lzm0x219/template-vite-react](https://github.com/lzm0x219/template-vite-react/tree/1104836a9e4bf4fb42c90e15809a78a4eb79dd82) | `1104836a9e4b` | React/Vite and Node >=24 detected | CI model request timed out and consumed its reserved task budget. | Isolated real Docker install failed. Saved diagnostic omits the terminal error. |
| [jakedahn/nextjs-boilerplate](https://github.com/jakedahn/nextjs-boilerplate/tree/b032c55a473d328d0d5518c315fdc97b7b91ddf8) | `b032c55a473d` | Next.js full-stack app detected | Source-history guard rejects normal Zod authentication schema as a secret. | Not reached. |
| [vercel/nextjs-postgres-nextauth-tailwindcss-template](https://github.com/vercel/nextjs-postgres-nextauth-tailwindcss-template/tree/fe026711c19ac3ee4589c86a738f59b84e611559) | `fe026711c19a` | Next.js full-stack app detected | All planned file tasks passed. CI needed three attempts. | Plan rejected because pnpm has no pinned packageManager version. Environment form/build not reached. |
| [fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template/tree/aabb90126964a164e02aff39e7c385bb0598236d) | `aabb90126964` | FastAPI backend and React/Vite frontend detected | Source-history guard rejects ordinary credential-related source expressions. | Not reached. Bun and external-service handling remain untested here. |
| [guipas/nextjs-minimal-typescript-tailwind](https://github.com/guipas/nextjs-minimal-typescript-tailwind/tree/31aab6fcf9d788b44cdde533ad0a17b8b84c25e6) | `31aab6fcf9d7` | Next.js SSR role with unresolved confirmation question | Start returns HTTP 409 ambiguous_plan despite explicit native strategy. | Not reached. |
| [kinsta/hello-world-nextjs](https://github.com/kinsta/hello-world-nextjs/tree/77373b4634b5f71d9de564f2a3b1913789e79859) | `77373b4634b5` | Framework detected, but quoted static export misclassified as SSR | The false SSR question blocks start with HTTP 409 ambiguous_plan. | Not reached. |

The first seven cases ran sequentially, followed by bounded resume attempts and two additional small Next.js cases. Stage 1 worktree create, inspect, and remove succeeded on all nine repositories. Eight of the nine explicit optional-AI requests succeeded. One timed out and retained deterministic findings. Some default analyses also requested the model automatically for ambiguity; `use_llm: false` is not a strict model-disable switch in this implementation.

## Additional runtime and approval controls

The Microsoft repository's live-generated, accepted Dockerfile passed a separate real Docker build, startup observation, and HTTP probe. This is component evidence, not an end-to-end pass, because its service run's CI task remained blocked.

The pnpm Vite repository also reached a separate real Docker execution. Its install command failed after dependency downloads. The saved failure contains leading download progress rather than the final error. Its Node requirement is `>=24`, while the default execution image was Node 22. The version mismatch is observed, but it is not claimed as the established cause of this failure.

An initial controlled run used VoxOps's existing simulated model on the unchanged Microsoft application. It reproduced a real COPY failure, made one targeted Dockerfile repair, built successfully, and then correctly rejected startup because the fixture's narrow Dockerfile omitted `models.py`. This was a test-fixture limitation, not a product defect. Publication remained blocked.

The controlled replay completed Stage 1, Stage 2, a real failed COPY build, targeted repair, successful real Docker build/start/HTTP checks, final approval, and publication to its disposable local bare Git remote. The verified branch was `voxops/c43483306ae345f28ed9f973f409a0f2` and the verified SHA was `eb1858573939d3359fc86112deb73840ac56e3e5`. This was not a GitHub push or a live-model repair test.

The corrected replay retained the intentional first COPY failure. Its repaired response replayed the exact Dockerfile already accepted from the live model and proven by the isolated Docker test. The model boundary was explicitly simulated for this control. API routing, source review, Docker, repair selection, attempt accounting, approval, and Git operations were not simulated.

Six API boundary controls passed: normal transcript acceptance, secret-assignment transcript rejection, forbidden repository path rejection, empty-path rejection, cross-origin rejection, and same-origin acceptance. Seven live cases also exercised publication-before-verification rejection. Actual run evidence records each HTTP status.

## Confirmed bugs

### B1. P1. Ordinary credential-related code is mistaken for a secret

**Affected code:** `src/voxops/artifacts/security.py:16-22,266`.

**Observed:** `jakedahn/nextjs-boilerplate/src/lib/auth/schemas.ts` contains a normal Zod schema. Its `credentialsSchema = z.object(...)` and `password: z.string().min(...)` expressions trigger `secret_detected`. The full-stack FastAPI template also contains ordinary expressions such as password hashing and token decoding that this regex rejects. Publication preflight stops before generation.

**Minimal reproduction:** `check_secret("auth.ts", b"const credentialsSchema = z.object({ password: z.string().min(8) })")` raises `AppError("secret_detected", ...)`. So does `const token = jwt.decode(value)`. Neither example embeds a credential value.

**Expected:** Ordinary source expressions do not count as literal credentials. Secret literals and private files still remain blocked. The regex accepts unquoted expression text as if it were an assigned secret.

**Evidence:** `controls.json`, `reproductions.json`, and the pinned upstream schema.

### B2. P2. Execution ignores the configured file-size limit

**Affected code:** `src/voxops/artifacts/service.py:501` and `src/voxops/execution/environment.py:317`.

**Observed:** The React/Vite TypeScript repo has a 187,294-byte `package-lock.json`. Planning and source capture accept it with `max_file_bytes=1048576`. Execution then returns `file_too_large` before generating files. The required-input check calls `safe_read` without the configured limit and gets its 131,072-byte default. Environment source hashing separately hardcodes 131,072 bytes and reproduces the same rejection.

**Expected:** The same supported configured limit applies consistently through planning, generation preparation, and environment discovery.

**Proof:** `capture_source(source, 1048576)` and `safe_read(source, "package-lock.json", 1048576)` succeed. The default `safe_read` and `EnvironmentManager.source_digest` fail on the same unchanged file.

**Evidence:** `reproductions.json` and the Vite TypeScript run in `results.json`.

### B3. P2. A quoted Next.js static-export key is missed

**Affected code:** `src/voxops/repository/filesystem_scanner.py:214`.

**Observed:** Kinsta's real `next.config.js` declares `'output': 'export'`. The scanner does not emit `next_static_export`. Stage 1 classifies it as SSR and adds a runtime-confirmation question. Stage 2 cannot start. This is a valid JavaScript object key.

**Proof:** `source_evidence("next.config.js", original_text)` misses the export. Passing otherwise identical text with the quotes removed from the key detects it. Only the probe's in-memory string changed; the downloaded repository was not edited.

**Expected:** Literal `output`, `'output'`, and `"output"` keys describe the same supported static-export configuration.

**Evidence:** `controls.json`, `pipeline-next.log`, and the pinned Kinsta config.

### B4. P2. Context selection makes a small FastAPI package impossible to generate

**Affected code:** `src/voxops/artifacts/planner.py:72-90` and `src/voxops/artifacts/context_builder.py:37,77-100`.

**Observed:** The small packaged FastAPI example creates a 51,573-character packet for its Dockerfile job, including 46 files and 20 test files. The largest supported context setting is 32,000. Both Dockerfile and dockerignore jobs stop with `context_too_large` before any model request. Increasing the supported setting cannot unblock this case.

**Expected:** Required build-file inventory does not force the full contents of every test into a deployment-file model prompt. The existing context limit can remain enforced while the relevant evidence is selected or compacted.

**Proof:** The normal call fails at 32,000. A diagnostic-only call with an unvalidated measurement limit returns the 51,573-character packet and its file-size list. That bypass was not used to generate files or count a pipeline pass.

**Evidence:** `reproductions.json` and the packaged FastAPI run in `results.json`.

### B5. P2. An ordinary Next.js starter has no API path past the SSR question

**Affected code:** `src/voxops/frameworks/nextjs.py:40-42`, `src/voxops/artifacts/planner.py:62`, and `src/voxops/artifacts/service.py:383-385`.

**Observed:** The minimal Next.js starter has `next build` and `next start`. Stage 1 asks `No static export: confirm SSR/runtime requirements.` The harness explicitly chooses `native`, but the question remains. Starting the returned plan fails with HTTP 409 `ambiguous_plan`. The current request schemas offer no answer or confirmation field, and planning reruns analysis, so a client cannot resolve this by confirming the detected server strategy.

**Expected:** A valid explicit server-strategy confirmation has a supported way to resolve that question. This does not require treating genuinely conflicting export/server evidence as safe.

**Evidence:** `pipeline-next.log` and the `next_minimal` plan and start response in `results.json`.

### B6. P2. A model-invented GitHub Actions SHA passes CI validation

**Affected code:** `src/voxops/artifacts/validators.py:234-243`.

**Observed:** The accepted CI file for Vercel's Next.js database template contains `actions/setup-node@49933ea5288caeca8642d1e84afbd6f7d6820020`. The validator accepts its name and 40-character shape. GitHub cannot resolve that commit. The real similar commit is `49933ea5288caeca8642d1e84afbd3f7d6820020`.

**Proof:** The GitHub connector rejects the generated reference and resolves the real one. The public GitHub commits API returns HTTP 422 for the generated SHA and HTTP 200 for the real SHA. The workflow's checkout commit also resolves correctly. These were read-only checks; GitHub Actions was not run.

**Expected:** Generated references described as approved pinned actions are grounded in real commits, rather than accepted solely because the string looks like a SHA. The current generated workflow would fail when GitHub tries to resolve the action.

**Evidence:** `generated-ci-actions.json` and `ci-sha-checks.json`.

### B7. P2. Build diagnostics lose the actual error at the end of output

**Affected code:** `src/voxops/execution/runner.py:754`.

**Observed:** The failed real Vite install is saved as leading package-download progress. `_require_success` retains only `result.output[:1500]`. The builder's container log does not recover the exec command's missing terminal error.

**Proof:** A `CommandOutcome` containing 100 progress lines followed by `ACTUAL_FAILURE_AT_END` produces an error message without that final marker. This reproduces the same data loss without repeating the dependency download.

**Expected:** Bounded redacted diagnostics preserve the terminal error, normally the tail of the command output, so a user can understand or correct a failed build.

**Evidence:** `stage3-isolated.json` and `reproductions.json`.

### B8. P3. A successful run keeps an obsolete publication error

**Affected code:** `src/voxops/artifacts/service.py:968`, the successful execution path, and the publication result route.

**Observed:** The successful replay first attempted publication without final approval, correctly receiving `approval_required`. It then completed verification, received final approval, and published its branch with a verified SHA. The saved run is `ready_for_deployment`, but its `errors` list still contains `approval_required`. `GET /result` returns that obsolete error beside the successful receipt.

**Expected:** A successful retry no longer reports the earlier failure as a current run error. Attempt history can retain previous failures separately.

**Evidence:** The `control_fastapi_replay` result in `results.json`, including the HTTP 200 publication and final saved error list.

## Environmental and intentional blockers

NVIDIA timeouts occurred in both optional analysis and artifact generation. A timed-out artifact call is conservatively charged its whole remaining token reservation. The Microsoft and pnpm Vite CI jobs therefore could not make another request on explicit resume, despite having fewer than three content attempts. Those outcomes are recorded as provider/budget limitations, not invented source bugs.

The Vercel pnpm repository lacks a pinned `packageManager` version. The native execution planner intentionally rejects that configuration. The full-stack template's Bun frontend was detected, but publication preflight blocked earlier, so this run does not establish Bun execution support. These guards were not disabled.

The FastAPI HTTP success checks basic reachability only. It does not prove its Redis-backed item endpoints work, and no Redis service was provisioned. External-service acknowledgement and missing-required-value submission were not reached by the live cases. Gnani audio, remote GitHub writes, GitHub Actions execution, and cloud deployment were not tested.

## Evidence and cleanup

Detailed local evidence is under `.audit/evidence/real-repos-2026-11-12/`. This directory is ignored by Git. `results.json` contains the ordered API steps and saved outcomes. `stage3-isolated.json` contains real execution attempts and resource records. The logs keep failed cases separate from controls. `pilot-setup-result.json` is an early harness source-selection mistake, not a product finding.

Downloaded repositories, temporary source imports, bare remotes, worktrees, and run storage are removed after the runs. Docker cleanup is checked against each test run's labels. The final `cleanup.json` records those checks, and `code-unchanged.json` compares every pre-existing file under `src`, `scripts`, and `tests` with its initial hash. The pre-existing uncommitted English comments are preserved. No application-code fixes, commits, pushes of VoxOps, or deployments are performed.

## Approved corrections after testing

The user approved these corrections after reviewing the findings. The original results above describe the earlier code and have not been rewritten as successful runs.

- B1 separates literal credentials from ordinary source expressions. Existing token/key detection, protected paths, and literal-default checks remain active. Unquoted configuration credentials are checked separately from source code.
- B2 applies the configured file-size limit to required-input reads and environment discovery. Default source reads, stored files, generated text, and frozen input copies now allow up to 1 MiB. Explicit smaller settings remain enforced.
- B3 recognizes quoted Next.js `output` keys.
- B4 keeps the complete required-path inventory while selecting entrypoints, manifests, configuration, and existing generated files for model input. Test contents are excluded unless the requested correction targets that file. Context defaults to 64,000 characters and can be configured up to 128,000.
- B5 treats an explicitly selected native or container strategy as confirmation of the ordinary SSR question when build and start commands are known. Conflicting export/server evidence still blocks generation.
- B6 supplies and validates the same maintained list of six official action commits. Each commit was resolved against its official GitHub repository before being added. Updating this list requires checking replacement commits. Changes to the list invalidate earlier publication/verification policy comparisons.
- B7 preserves the final 1,500 characters of redacted command output rather than its first 1,500 characters.
- B8 clears resolved current errors after successful verification or publication. Their categories remain in warnings labelled `Resolved earlier error`.

Stage 2 generation and repairs now use `VOXOPS_ARTIFACT_MODEL_TIMEOUT`, defaulting to 180 seconds and configurable up to 300. Optional Stage 1 model calls retain their separate timeout. Each content request reserves at most one third of its file's token allowance. Unknown usage is still charged to that reservation. Generation and Stage 3 repairs retain three combined content attempts per file. Docker CPU, memory, build timeouts, and startup timeouts are unchanged.

The local ignored `.env` file's old size override was updated to 1 MiB. Explicit 64,000-character context and 180-second artifact timeout settings were added. Credential settings were not changed.

### Files changed for these corrections

Product code:

- `src/voxops/agents/artifact_agent.py`
- `src/voxops/artifacts/ci_actions.py`, newly added
- `src/voxops/artifacts/context_builder.py`
- `src/voxops/artifacts/generator.py`
- `src/voxops/artifacts/planner.py`
- `src/voxops/artifacts/review.py`
- `src/voxops/artifacts/security.py`
- `src/voxops/artifacts/service.py`
- `src/voxops/artifacts/validators.py`
- `src/voxops/core/settings.py`
- `src/voxops/domain/artifact_models.py`
- `src/voxops/execution/candidate.py`
- `src/voxops/execution/contexts.py`
- `src/voxops/execution/environment.py`
- `src/voxops/execution/environment_analysis.py`
- `src/voxops/execution/runner.py`
- `src/voxops/repository/filesystem_scanner.py`
- `src/voxops/repository/git_executor.py`
- `src/voxops/repository/snapshot.py`
- `src/voxops/storage/artifact_store.py`

Tests changed are `tests/test_pipeline_bugfixes.py`, newly added, `tests/test_repository_safety.py`, `tests/test_stage2_validation.py`, and `tests/test_stage3_service_integration.py`. Configuration and documentation changes are `.env.example`, the ignored local `.env`, `README.md`, `docs/stage2-usage.md`, and this follow-up. The English comments already present before these corrections were preserved.

### Verification and remaining limits

Before the corrections, focused regressions reproduced secret-expression rejection, the large-lockfile rejection, excessive context, missed quoted export keys, unresolved SSR confirmation, acceptance of the invented action hash, full-budget consumption on timeout, and stale publication errors. The command-tail regression was first corrected to construct a valid command result, then separately failed against the original implementation because `ACTUAL_FAILURE_AT_END` was missing.

Affected security, generation, environment, and service tests passed. The API publication test also passed with a 187-KiB lockfile, an injected Docker adapter, and a disposable local bare Git remote. A subsequent full-suite run found one test still relying on the former size default. Giving that test an explicit 128-KiB limit preserved its original boundary check and passed its focused rerun.

A bounded live CI-generation check using the configured NVIDIA Mistral model passed after two content attempts. Each request reserved 6,666 tokens; reported usage was 1,988 and 2,221 tokens. This check used a disposable FastAPI sample and did not execute GitHub Actions or publish a branch. Its temporary files were removed.

The nine original real repositories and real Docker builds were not rerun for this correction. Unknown model usage remains conservatively charged, and an oversized prompt or exhausted shared budget can still block work. The action list requires manual maintenance. Environment discovery remains a limited static analyzer; these fixes do not add general application-code repair or broader syntax analysis. No commits, GitHub pushes, merges, or deployments were performed for these corrections.

Final regression result: **353 passed** in 70.81 seconds. Ruff and `git diff --check` passed. The 40 warnings are existing dependency deprecation warnings.
