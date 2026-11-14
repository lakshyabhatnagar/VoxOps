# Stage 2 verification

Historical Stage 1–3 evidence below describes the original implementation. Stage 4
replaces operational JSON files and filesystem locks with MongoDB records and a
single-process execution boundary. See [Stage 4 decisions](stage4-decisions.md).

Raw logs and demo results are local working records under `.audit/`, which is excluded from Git. The commands below reproduce the checks.

Verified on 2026-11-05 with Python 3.14.8. The Stage 1 baseline was 28 passing tests. The current integrated suite is **110 passed**, with 24 upstream LangSmith/PydanticAI deprecation warnings. The warnings do not indicate skipped tests. `.audit/stage2-pytest.txt` contains the actual result.

## Commands actually run

Commands used the checked-in uv environment. This workspace required `UV_CACHE_DIR=/tmp/deploygraph-uv-cache` because the normal home cache is read-only. The sandbox also stalled Python worker-thread completion, so pytest and HTTP demos ran outside that restriction against disposable local resources. No cloud or GitHub write was authorized by that adaptation.

| Command | Observed result |
| --- | --- |
| `uv sync --locked` | Final check resolved 65 packages and checked 63 installed packages. The earlier editable install also passed. |
| `uv run ruff check src tests scripts` | All checks passed. |
| `uv run pytest -q` | 110 passed, 24 deprecation warnings. |
| `uv run python scripts/check_comments.py` | Checks two explanation lines directly above every Python function and class. The user later replaced the original Why/How labels with plain-English explanations. |
| `uv run python scripts/demo_stage2.py --help` | Listed offline, fixture, bootstrap, reject and keep options. |
| `node tests/test_api_tester.mjs` | Request, rendering, ID and cleanup-policy checks passed. |
| `docker buildx version` | Buildx v0.38.1 detected. No build/check was executed. |

`.audit/stage2-sync.txt`, `.audit/stage2-ruff.txt`, `.audit/stage2-comments.txt`, and `.audit/stage2-demo-help.txt` are retained locally. actionlint was not installed. No paid model, live GitHub MCP, real GitHub repository creation/push, GitHub Actions run, Docker build or cloud deployment was tested.

## Acceptance evidence

The tests use actual scanners, PydanticAI TestModel/FunctionModel, LangGraph, private storage and disposable real Git repositories. Fake MCP adapters or an HTTP MockTransport replace external GitHub operations explicitly. They are never the production fallback.

| Goal | Current proof |
| --- | --- |
| A1 | `test_framework_strategy_plans` covers FastAPI container tasks. The HTTP and CLI demos inspect four approved artifacts and preserve source. |
| A2 | The Vite static plan and final Vite CLI demo produce CI and .gitignore, with no Dockerfile. |
| A3 | Planner tests cover SSR/native, SSR/container, rejection of false static export, and an actual explicit Next export with no API route. |
| A4 | The real mixed monorepo fixture yields separate units, one CI owner and root build contexts that retain inherited dependencies. |
| A5 | `test_existing_preserved_and_unavailable` preserves valid Dockerfile and CI bytes/hashes with zero model attempts. |
| A6 | `test_existing_invalid_artifact_gets_minimal_proposal` repairs one Docker instruction without changing source. Ignore repair preserves comments. Actual review/approval tests gate application publication. |
| A7 | The real Send graph waits until three model calls overlap. Only a failing Docker task retries, at most three times; successful siblings remain. CI waits for its dependencies. |
| A8 | Typed planner tests reject duplicate paths, unknown dependencies and cycles. The keyed reducer rejects conflicting results. |
| A9 | Missing credentials produce model_unavailable with no fake proposal. Independent recovery testing later configured a model and preserved completed sibling records. |
| A10 | Tests cover secret names/URLs/headers, .env/npm auth/key boundaries, symlinks, oversized context, typed wrong output paths and secret-bearing model output. Source patches that introduce probable secret names in browser-public configuration fail even without literal credential values. Agents have no execution/MCP tools; repository text remains data. |
| A11 | Real Git tests bootstrap exact selected binary/CRLF/executable files through separate private-baseline approval and preserve unrelated staged index bytes. Rejected baseline HTTP tests create nothing. |
| A12 | Real Git tests compare original source bytes, HEAD and branch while publishing only the recorded VoxOps branch. |
| A13 | Snapshot tests and the actual HTTP source-selection flow include one selected dirty file, disclose omissions and preserve original tracked/untracked files. Hidden/dependency omissions are also disclosed. |
| A14 | Rejection, changed source, bytes, modes, branch, plan and policy all prevent commit/push. Rejected runs cannot be revived by source selection. |
| A15 | Lost MCP creation response, baseline HEAD-update interruption, failed push after commit and repeated publication converge to one repository/commit. |
| A16 | Every successful local publication compares actual remote SHA with the approved commit and returns deployment_executed=false. |
| A17 | All original Stage 1 fixture/API/security/worktree tests remain in the passing integrated suite. The existing frontend script checks also pass. |

Implementation checkpoints 0 through 7 have corresponding code and behavioral evidence. Checkpoint 8 adds all five Stage 2 documents, preserves the Stage 1 historical documents and runs the comment/lint gates. [Planner tests](../tests/test_artifact_planner.py), [generation tests](../tests/test_artifact_generation.py), [validation tests](../tests/test_stage2_validation.py), [Git tests](../tests/test_stage2_git.py) and [HTTP tests](../tests/test_stage2_end_to_end.py) contain the assertions.

## Actual local publication evidence

These are local bare repositories with fake GitHub metadata. The example GitHub URL in their result is not a real repository created by this work. Source preservation and exact remote SHA equality were asserted by each demo.

| Final demo | Result and local remote SHA |
| --- | --- |
| Existing committed FastAPI | `e93fd864a326c82d30aded69153c07e811c47b5f`, ready_for_deployment. |
| Initial private-baseline flow | `3ce3436411db96e44e04a88d0110ab230b19d300`, ready_for_deployment. |
| Vite static | `a4a7ca4a02a4a7423e00168c443cbac9ea538169`, ready_for_deployment, no Dockerfile. |
| Rejected artifact review | rejected, null result, no artifact branch publication. |

Reproduce with `uv run python scripts/demo_stage2.py --offline`, adding `--bootstrap`, `--fixture vite_only`, or `--reject`. The final retained directories are `/tmp/voxops-stage2-final-existing`, `-bootstrap`, `-vite`, and `-reject`. Their sanitized `.audit/stage2-demo-existing.json`, `.audit/stage2-demo-bootstrap.json`, `.audit/stage2-demo-vite.json` and `.audit/stage2-demo-reject.json` results are recorded. SHAs vary on a new run.

A separate Uvicorn process was driven over actual `127.0.0.1:18766` TCP HTTP through scan, plan, source review, artifact generation, exact approval, publish and result. It used the same explicit fake model/MCP boundaries and a real local bare remote. `.audit/stage2-live-http.json` records the exact SHA and source-preserved assertion. Its source, records, worktree and bare remote are retained at `/tmp/voxops-stage2-tcp-final`. Temporary probe scripts are outside the repository; the checked-in demo and tests are the reproducible verification interface.

After the final browser-public secret guard, `uv run python scripts/demo_stage2.py --offline --keep /tmp/voxops-stage2-release-check` passed again. The local result file `.audit/stage2-demo-release.json` records matching commit and remote SHA `d1f111a4ea1752afd7c1063ca79e31b6148d0f32`, preserved source and `deployment_executed=false`.

## Independent review and limits

Independent reviewers reproduced defects in compound-secret matching, malformed YAML types, source-excluding ignore rules, source-branch binding, omitted-path disclosure, rejection revival and crash token accounting. Those defects were fixed, and original probes/regressions were rerun. Modern curated MCP permission metadata was also checked against the official tool implementation and added to offline transport tests. The audit trail is `.audit/stage2.tsv`.

The final independent `gpt-6-luna` audit reviewed the contract, code, evidence, documentation and this run's transcript. Its supplement verified the final source fingerprint and browser-public secret guard and reported no remaining verified findings. The audit also confirmed the decision trail against the transcript. A preliminary claim of a missing cross-model review was corrected because that audit itself supplied the required review. Live authenticated MCP behavior remains untested.

Static validators are conservative heuristics, not full Docker/Gitignore interpreters or proof of arbitrary script safety. Python/JSON source patches receive syntax checks; JS/TS patches do not have a full parser. Provider configuration is skipped without a target and blocked for an unsupported target. Git supports standalone HTTPS repositories with bounded history/files; unsupported paths/history stop explicitly. Token budgets use reported usage and conservative reservations; one provider response can exceed a usage threshold before the limit is observed. An absent-or-same remote check plus normal non-forced push is not an atomic create-only lease against every concurrent actor.

The installed pstack skills ran through their Codex mapping. Model roles were declared locally; no global model setup, project merge or project push was performed. Mandatory comments overrode the no-comments advice. Stage 3 execution isolation, Stage 4 durable supervisor, Stage 5 providers/deployment and Stage 6 product/auth/billing/voice remain deferred.
