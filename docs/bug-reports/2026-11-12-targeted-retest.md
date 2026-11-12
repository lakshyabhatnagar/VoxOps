# Targeted retest of five previously blocked repositories

Five of the original nine repositories were tested again at the same upstream revisions, after the approved corrections. All five passed the selected Stage 1 and Stage 2 checks. This was not a full pipeline rerun or a full regression-suite run. No product code was changed.

## Scope and results

The original temporary repositories had been deleted. Each selected revision was downloaded again, then its unchanged files were imported into a disposable local Git baseline. Detection ran once without an AI model. The test then ran the real planner, source-history guard, normal source-review approval when required, worktree preparation, required-input reads, context construction, live NVIDIA generation through `Stage2Graph`, cross-file validation, and application of accepted proposals to the isolated worktree.

Generation used `nvidia:mistralai/mistral-large-4-1t-a52b`. Settings were 1 MiB per file, 64,000 context characters, a 180-second content-request timeout, 20,000 tokens per file, and one generation worker. Final publication and Docker verification were deliberately outside this test. GitHub metadata was simulated only for local source/worktree preparation; all Git transport writes went to disposable local bare remotes.

| Repository and pinned revision | Previous blocker | Targeted result | CI model attempts |
| --- | --- | --- | --- |
| [mgershenzon/fastapi-example](https://github.com/mgershenzon/fastapi-example/tree/b9b7282c3a9566e2a0888e8a92ab19acf613dd65) | Dockerfile input was 51,573 characters and exceeded the supported limit. | Passed. Dockerfile input is now 9,821 characters. All Stage 2 files and cross-file checks passed. | 2 |
| [emisasal/vite-react-typescript](https://github.com/emisasal/vite-react-typescript/tree/3bbadfe25845c29016eb704cf23563ffaed27104) | The 187,294-byte npm lockfile was rejected by an inconsistent 128-KiB limit. | Passed. Required-file reads, Stage 2 generation, cross-file checks, and environment source hashing accepted the unchanged lockfile. | 1 |
| [jakedahn/nextjs-boilerplate](https://github.com/jakedahn/nextjs-boilerplate/tree/b032c55a473d328d0d5518c315fdc97b7b91ddf8) | Ordinary authentication schema expressions were rejected as secrets. | Passed. The source-history guard accepted the original files. Generation and cross-file checks passed. | 1 |
| [guipas/nextjs-minimal-typescript-tailwind](https://github.com/guipas/nextjs-minimal-typescript-tailwind/tree/31aab6fcf9d788b44cdde533ad0a17b8b84c25e6) | The SSR question remained unresolved despite an explicit server strategy. | Passed. Stage 1 retained the SSR question, and choosing `native` resolved it in the plan. Generation and cross-file checks passed. | 2 |
| [kinsta/hello-world-nextjs](https://github.com/kinsta/hello-world-nextjs/tree/77373b4634b5f71d9de564f2a3b1913789e79859) | Quoted `'output': 'export'` was missed and the app was classified as SSR. | Passed. Stage 1 identified a static frontend, the static strategy had no unresolved question, and generation and cross-file checks passed. | 1 |

## Model attempts and remaining observations

Ten live model requests were made across these five cases. Existing ignore files were accepted or corrected deterministically where supported, so not every file required a model request. Every model request reserved 6,666 tokens. No provider timeout or transport error occurred.

The packaged FastAPI example's first dockerignore proposal failed the credential-file exclusion check. Its second proposal passed. Its first CI proposal and the minimal Next.js starter's first CI proposal failed the explicit read-only permissions check. Both passed on their second attempt. These were rejected model proposals followed by successful correction, rather than new established source-level bugs. All five accepted CI files passed validation against the maintained verified action list.

Two requests took longer than the former 60-second timeout. The packaged FastAPI dockerignore requests took 76.71 and 63.53 seconds. The first completed with validation feedback; the second completed successfully. This provides direct evidence that the longer timeout is useful on this provider. It does not guarantee future requests will finish within 180 seconds.

The first test script omitted a required source-review approval for the Vite and authentication starters. Those two cases stopped before generation at `approval_required`. The script was corrected to use the existing approval mechanism, and only those two cases were repeated. Their initial setup errors are retained separately and are not counted as product defects or additional unique repositories.

No new source-level blocker was established in the tested parts. Passing static CI validation does not prove the workflow will run successfully on GitHub Actions. Docker builds, runtime checks, external services, final approval/publication, Gnani audio, and the four unselected repositories were not retested. Existing evidence for those areas was not replaced by these results.

## Evidence and cleanup

Detailed local evidence is under the ignored directory `.audit/evidence/real-repos-2026-11-12/targeted-rerun/`:

- `results.json` records the five final cases, exact plans, context file selections, attempts, validations, and generated CI text.
- `run.log` and `setup-retry.log` record the initial pass and the two setup retries.
- `setup-retries.json` preserves the two initial approval setup errors.
- `model-request-summary.json` records request durations, token usage, and outcomes.
- `harness.py` contains the corrected repeatable test script. It depends on the local repository catalog from the original run and writes no GitHub branches.
- `cleanup.json` confirms deletion of both temporary directory sets and unchanged VoxOps source, scripts, and tests.

All downloaded repositories, imported sources, worktrees, run storage, and disposable bare remotes were deleted. Source contents were unchanged for all five repositories. No Docker resources were created. No full regression suite, VoxOps commit, GitHub push, merge, or deployment was performed.
