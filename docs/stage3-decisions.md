# Stage 3 implementation decisions

- Verification belongs before the final Stage 2 artifact review. Initial baseline approval remains
  separate. `GitPublisher` remains the only commit/push owner.
- Existing adapters, Docker execution, environment discovery, and content workers are reused.
  `ArtifactService` connects them without introducing another workflow engine.
- One digest-bound form collects genuinely missing essential values. Defaults and local values
  avoid prompts; unchanged configuration survives an artifact-only repair without another scan.
- Values remain volatile. Persisted records contain names and random generation markers. Restart
  may require re-entry and reapproval, without reconstructing secret values from saved JSON.
- Repairs retain the same task ID, attempt ledger, shared model budget, and total three content
  attempts. Applied-parent hashes authorize replacement while original hashes preserve provenance.
- Grounded external dependency failures may be incomplete. Exact acknowledgement and final diff
  approval are both required. Generic errors and build failures remain failures.
- `ArtifactRun` now links execution and operation ownership. `ExecutionRun` holds per-unit results,
  attempts, cleanup records, and an attestation. Old final approvals without verification fail closed.
- Existing FastAPI, React/Vite, and Next.js strategies remain supported. Cloud deployment, durable
  Stage 4 workflows, accounts, and arbitrary business-logic repair stay outside this MVP.

Recovery finished the existing API/publication connection and migrated legacy publication fixtures
through simulated Docker. The executable demo exposed three small integration corrections: stale
session resolution metadata, environment-read secret-scan false positives, and premature image intents.
