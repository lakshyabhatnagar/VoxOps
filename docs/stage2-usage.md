# Stage 2 artifact generation

Stage 4 now owns workflow orchestration. Use the [current workflow instructions](../README.md#run-a-github-workflow).
The former artifact execute/publish HTTP routes and Stage 2 CLI executor are removed.

Stage 2 remains the internal planning, task-scoped generation and validation algorithm.
MongoDB stores plans, runs, canonical task histories and generated content-addressed bytes.
The generator receives selected safe source text, detected commands, previous candidates
and validation/repair feedback. It has no repository tools or environment values.

A user workflow attempt has three content attempts per file. An explicitly retryable
model call can receive one additional retry inside that allowance. New user attempts
have new workflow IDs and allowances; earlier attempts and global spending remain.
Successful unchanged tasks are reused, while repair targets only an authorized planned
file and records its applied parent hash. Source patches still require explicit authorization.

Prior Stage 2 publication/security evidence is retained as historical documentation.
Current executable workflow evidence is in [Stage 4 verification](stage4-verification.md).
