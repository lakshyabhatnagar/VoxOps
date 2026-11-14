# Stage 3 verification

Historical Stage 3 commands below describe the previous executor. Its CLI scripts
and execute/publish routes have been removed; use the Stage 4 workflow and tests.

The recovery checks the integrated API, one representative real Docker journey, and a final
regression/lint pass. Simulated Docker is identified explicitly.

`tests/test_stage3_service_integration.py` exercises required configuration, invalid submission,
resume, final review, approval, and a verified local bare-Git push. Its incomplete-service case
requires acknowledgement before publication. The API, service, runner, approvals, and Git are real;
Docker is injected. Legacy publication fixtures now follow the legitimate verification path.

Focused results during recovery:

- Corrected dirty-snapshot fixture plus API journey: **2 passed**.
- Incomplete publication plus affected environment API/session tests: **12 passed**.
- Environment-read correction: **48 secret-boundary tests passed**.
- Image-intent correction: **34 Docker-driver tests passed**.
- Existing Node API-tester and environment-form checks: passed.

The real smoke command executed successfully from the integration checkout:

```sh
PYTHONPATH=src /home/lakshya/VoxOps/.venv/bin/python scripts/demo_stage3.py
```

It used real loopback HTTP, Docker build/start/readiness, targeted repair, final approval, and a local
bare Git remote. Only model responses and GitHub metadata were simulated. The observed receipt was:

```text
branch: voxops/bf5841e1ea2248d68ed94d523d3e80f7
remote SHA: 72dc3782e20a040dced3ec223d121ee2a27a4414
verification: passed
Dockerfile content attempts: 2
source unchanged: true
deployment_executed: false
```

The demo checked resource cleanup and absence of values in run/review JSON. Its temporary remote
was removed afterward. Existing successful Vite, Next.js native/container/static, and mixed-repository
Docker evidence is reused for unchanged framework paths; these larger builds were not repeated.

Reproduce the final root-checkout checks:

```sh
uv run pytest -q
uv run ruff check src tests scripts
node tests/test_api_tester.mjs
node tests/test_environment_form.mjs
```

Executed against the integrated root checkout:

- `.venv/bin/python -m pytest -q`: **316 passed, 33 dependency deprecation warnings**, 181.54 seconds.
- `.venv/bin/ruff check src tests scripts`: **All checks passed**.
- Both Node commands above: **passed**.
- `git diff --check`: **passed**.

All seven previously deferred publication tests pass through legitimate verification fixtures.
No live paid-model/GitHub access, business-logic coverage, or cloud deployment is claimed.
