# Stage 3 environment resolution

The resolver combines Stage 1 names with bounded static examination of relevant Python and
JavaScript files. It never imports application settings code. An unchanged configuration reuses
its scan after submission; artifact-only repairs do not rescan settings.

Common supported patterns include Python `os.environ[...]`, `os.getenv`, `os.environ.get`,
Pydantic Settings fields and `Field(default=...)`, supported aliases/inheritance, and JavaScript
`process.env.NAME` or `import.meta.env.NAME` with literal fallbacks or explicit required guards.
`.env.example` names are weak evidence, not proof that every listed key is mandatory.

In Pydantic v2, `str | None` without a default remains required. Usable defaults and applicable
`None` avoid a required prompt. Dynamic or conflicting requirements block rather than guessing.

The local reader follows discovered settings scopes and environment-file order. Later applicable
entries replace earlier entries; submissions can replace local values. Empty, placeholder, invalid,
oversized, symlinked, or unsafe inputs cannot satisfy required fields. Host interpolation is disabled.

The UI groups genuinely missing required inputs by unit and phase. Configured, optional, and
defaulted names appear as status text. The browser and server both validate required inputs.

```json
{"schema_digest":"<digest from env-form>","values":{"<field_id>":"<enter locally>"}}
```

GET returns names, evidence, and states, never values. `SecretSession` keeps values in memory;
private temporary files supply scoped runtime or supported BuildKit secret injection. Values stay
out of model prompts, persisted records, review diffs, and Git. Known values and credential patterns
are redacted from diagnostics, including rotated values.

`VITE_` and `NEXT_PUBLIC_` values are public build configuration. Unsupported secret build injection
fails rather than baking credentials into an image. After restart, lost supplied values require
re-entry; defaults can be recovered and unrelated unchanged results remain eligible for reuse.
Unusual dynamic syntax may remain ambiguous and require clarification.
