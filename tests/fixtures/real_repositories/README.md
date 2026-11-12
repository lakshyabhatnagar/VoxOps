# Seven offline repository pipeline cases

These tests retain the exact source files from seven previously tested GitHub revisions. The compressed sources total about 620 KiB. No repository download, API key, Node installation, or Docker engine is needed for the default run. Git is required.

From the VoxOps root:

```bash
# Seven cases from Stage 1 through approval and local Git publication, where supported.
.venv/bin/pytest tests/test_real_repositories.py -q

# Start directly at one stage, using the saved inputs from earlier stages.
.venv/bin/pytest tests/test_real_repositories.py --repo-stage=1 -q
.venv/bin/pytest tests/test_real_repositories.py --repo-stage=2 -q
.venv/bin/pytest tests/test_real_repositories.py --repo-stage=3 -q

# Run just the large-lockfile case.
.venv/bin/pytest tests/test_real_repositories.py --repo-stage=2 -k vite_typescript -q
```

`--repo-stage=1` runs detection. `--repo-stage=2` loads saved detection results, then runs planning, source checks, and the real Stage 2 graph with offline model responses. It stops before environment discovery and Docker verification. `--repo-stage=3` loads the saved plan and accepted file candidates, then runs verification and approval/publication without running detection or requesting model output. The default `all` runs the connected workflow through the normal HTTP routes.

Stage 2 and later use real disposable Git repositories and worktrees. Publication goes only to a temporary bare Git repository on this computer. It never pushes to GitHub or changes VoxOps's branch.

## What each case checks

| Case | Upstream repository | Main regression covered | Default Stage 3 expectation |
| --- | --- | --- | --- |
| `fastapi_microsoft` | microsoft/python-sample-vscode-fastapi-tutorial | FastAPI command detection and container workflow. | Verification, approval, verified local branch/SHA. |
| `fastapi_package` | mgershenzon/fastapi-example | Packaged startup command and avoiding excessive model context. | Stop at the environment form because dynamic settings cannot be resolved statically. Publication stays blocked. |
| `vite_typescript` | emisasal/vite-react-typescript | The original 187,294-byte npm lockfile. | Reject the original interpolated `.env` values with `invalid_env_values`. Publication stays blocked. |
| `next_starter` | jakedahn/nextjs-boilerplate | Ordinary authentication expressions must not be mistaken for secrets. | Verification, approval, verified local branch/SHA. |
| `next_minimal` | guipas/nextjs-minimal-typescript-tailwind | Choosing a server strategy resolves the ordinary SSR question. | Verification, approval, verified local branch/SHA. |
| `next_static` | kinsta/hello-world-nextjs | Quoted `output: export` configuration. | Static verification, approval, verified local branch/SHA. |
| `next_database` | vercel/nextjs-postgres-nextauth-tailwindcss-template | Invented action hash rejected and corrected by the generator's retry path. | Unpinned pnpm version rejected with `INVALID_ARTIFACT`. Publication stays blocked. |

These expectations deliberately preserve the upstream sources. A passing test for one of the three blocked cases means the documented guard worked; it does not mean the repository was successfully built or published.

## Saved inputs

Each case directory contains:

- `upstream.json` with the repository URL components, exact Git revision, expected framework/role, and known Stage 3 outcome.
- `source.zip` with the unchanged tracked upstream working files, including any upstream license and public example configuration. It contains no `.git` directory or developer credentials. Tests extract a fresh copy into pytest's temporary directory.
- `stage1.json` with saved deterministic detection results for starting at Stage 2.
- `stage2.json` with the saved plan and explicit test responses for generated files. Five CI responses were retained from the live NVIDIA retest. The Microsoft Dockerfile was previously checked with real Docker. Other responses are authored fixtures, not evidence of live model generation or successful application builds.

The database case has a deliberately rejected first CI response containing the invented setup-node hash from the original investigation. Its next response is valid. Stage 2 and `all` assert that CI needed two content attempts. Stage 3 restores the accepted response directly.

## External boundaries

The default tests replay model responses and simulate Docker commands using the existing `DemoDocker`. Readiness still makes real HTTP requests to a tiny local responder, but that responder is not the upstream application. GitHub metadata is simulated. The planner, generator, validators, environment handling, runner, review/approval gates, Git commands, and remote-SHA checks are real.

These tests check VoxOps's coordination and known regressions. They do not check dependency installation, compilation of upstream applications, third-party services, live AI quality, Gnani audio, GitHub Actions execution, or cloud deployment. The three configuration limitations above are intentionally not fixed by this test work.

A real Docker run is opt-in for later. It can download dependencies and build upstream code:

```bash
.venv/bin/pytest tests/test_real_repositories.py \
  --repo-stage=3 --repo-real-docker -k fastapi_microsoft -q
```

The option changes only the Docker boundary. The model remains offline and all Git pushes remain local. Real Docker was not run while adding these tests.
