# Stage 1 decisions and source verification

## Gnani: explicit not-configured boundary

Reviewed official sources on **2026-11-02**. Gnani's current Prisma v3 page describes REST,
streaming and batch modes and directs developers to its account playground. It does not establish
an account-specific authenticated transport contract for this workspace. [Official Prisma v3](https://www.gnani.ai/resources/prisma-v3).

The older official API-service guide documents registration-issued token/accesskey/certificate,
gRPC TLS, and 16 kHz mono PCM16/WAV or AMR-WB. Its protocol and samples belong to that service;
they are not proof that an account provisioned for Prisma v3 uses the same contract.
[Official Gnani API-service guide](https://gnani-ai.github.io/API-service/).

No Gnani account contract or credentials were supplied. Therefore no live endpoint, headers,
streaming events, response fields, retries or audio constraints have been guessed or wired up.
`GnaniSpeechToText` implements the interface but raises `GnaniNotConfigured`. `TranscriptStub` is
functional and honestly labeled. Setting `GNANI_API_KEY` alone deliberately enables nothing.

To enable live STT in a later change, obtain the account's current versioned API/SDK documentation,
credential/authentication method, TLS and transport contract, supported languages, audio limits
and example response. Implement lifecycle, transient-only retry, timeouts, credential-safe errors
and audio validation against that exact contract, then perform an opt-in live smoke test using
non-sensitive audio. No paid/live smoke test is claimed for Stage 1.

## Structured optional inference

PydanticAI's `Agent(output_type=...)` validates specialist output. The model factory supports
Anthropic and OpenAI through provider-specific clients and environment-only keys. Other providers
can be added at that one seam; unsupported providers remain deterministic. Each scan permits one
request, a time budget and an output token cap; usage is recorded without raw prompts or responses.
[Official typed-output documentation](https://pydantic.dev/docs/ai/core-concepts/output/).

LangGraph uses fixed `StateGraph` nodes and conditional edges with explicit route maps; repository
data and model output cannot choose node destinations.
[Official conditional-edge API](https://reference.langchain.com/python/langgraph/graph/state/StateGraph/add_conditional_edges).

## Local safety and deliberate scope

The API binds loopback and also checks peer, Host and Origin. Allowed roots are mandatory nonempty
directories. No provider auth, remote Git creation, commits, pushes or project scripts are performed.
Worktree Git calls disable hooks, fsmonitor, content filters and recursive submodules. Source edits
are omitted from worktrees rather than silently copied or discarded. Dirty worktree cleanup is an
explicit opt-in. Detached source HEAD and nested source paths produce prerequisite errors.

The proposed layout is consolidated: one domain module, one routes module and one graph module
keep the small Stage 1 deliverable navigable. Adapters, repository safety, voice, storage and the
specialist retain separate boundaries. Initial inspection found no usable Git repository. Final
inspection found an unborn `main` branch with no commits or remotes. Implementation did not run
`git init`, create a remote or commit the root workspace. Real Git tests use disposable test-owned
repositories only. Root worktree creation correctly requires the user's initial commit first.
