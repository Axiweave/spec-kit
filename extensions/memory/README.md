# Spec Kit Memory

A [Spec Kit](https://github.com/github/spec-kit) extension that **recalls prior
specs and decisions from configurable memory tools** (e.g.
[memsearch](https://github.com/) memory-recall) before SDLC stages — so planning
and specification start from what the project already knows instead of a blank page.

By default it fires on **`before_specify`** (the primary recall point — surface prior
art *before* a spec is written) and on **`before_plan`** (ahead of Phase 0 research),
handing a short *Prior art* summary to each stage. The `recall` command is **read-only**:
it recalls and summarizes; it never writes specs, plans, or code.

It also runs **`speckit.memory.list-related-specs`** on **`after_plan`** — the one
writing step — which recalls the specs related to the feature and writes a compact
`## Related specs (from memory)` section (id + title) into the feature's `research.md`.
That section is the only thing it edits; it is idempotent and safe to re-run.

## Hooks

| Hook | Command | Mode | What it does |
|---|---|---|---|
| `before_specify` | `speckit.memory.recall` | read-only | **Primary** recall point — surfaces prior art *before* the spec is written |
| `before_plan` | `speckit.memory.recall` | read-only | Recall ahead of Phase 0 research, feeding the planner |
| `after_plan` | `speckit.memory.list-related-specs` | writes `research.md` | Writes the `## Related specs (from memory)` section (id + title) into the feature's `research.md` |

All three auto-run (`optional: false`) when the project has `settings.auto_execute_hooks: true`
(Spec Kit's default). `recall` stages are gated by `stages.*` in the config;
`list-related-specs` is gated by `settings.related_specs_enabled`.

## Why

Spec Kit hooks are command-scoped (`before_plan`, `before_specify`, …); the research
phase lives *inside* `/speckit.plan`, so the closest seam to "look up prior specs
before researching" is `before_plan`. This extension wires that seam and makes the
**set of memory tools a config list** rather than hardcoding one.

## Install

Clone the repo, then install it into your Spec Kit project from the local path:

```bash
git clone https://github.com/zaytsevand/spec-kit-memory
cd <your-speckit-project>
specify extension add --dev /path/to/spec-kit-memory
specify extension list      # ✓ Spec Kit Memory (v0.3.0) — 2 commands, 3 hooks
```

> **Catalog install** — `specify extension add <name>` resolves extensions from a
> *catalog* (an index), not from a bare repo URL. To enable
> `specify extension add memory` (or `--from <catalog-url> memory`), register a
> catalog that points at this repo first via `specify extension catalog add <url>`.
> Until then, the `--dev` path above is the supported install.

Installation copies the extension into `.specify/extensions/memory/`, materializes
`memory-config.yml`, merges the hook points into `.specify/extensions.yml`, and
registers both commands (`speckit.memory.recall`, `speckit.memory.list-related-specs`).

> Hooks auto-run only when the project has `settings.auto_execute_hooks: true` in
> `.specify/extensions.yml` (Spec Kit's default). Otherwise they are offered for
> manual execution.

## Configure

Edit `.specify/extensions/memory/memory-config.yml`:

```yaml
# WHICH memory tools recall invokes (run in order, results merged):
memory_tools:
  - command: "memsearch:memory-recall"
    description: "Semantic recall over past sessions, specs and decisions"
    # query: "<optional fixed search string; default = current feature topic>"
  # - command: "some-plugin:notes-search"
  #   description: "Search engineering notes"

# AT WHICH stages recall runs (the hook fires; this gates execution):
stages:
  default: true
  before_specify: true    # primary recall point — prior art before writing the spec
  before_plan: true       # research phase

settings:
  read_only: true
  fail_open: true         # never block a stage if a tool fails / finds nothing
  max_findings: 8
```

- **Add a memory tool** → append an entry to `memory_tools`. Any slash-command or
  skill the agent can run works (`plugin:skill` form, or a registered speckit command).
- **Recall at another stage** → set its `stages.<event>` to `true`. Supported events:
  `before_plan`, `before_specify`. (Add more hook points by editing the manifest.)
- **Pin a query** → set `query` on a tool to force a fixed search string instead of
  the auto-derived feature topic.

## How it works

The `speckit.memory.recall` command (`commands/recall.md`):

1. detects the firing event and loads config,
2. skips if that stage is disabled,
3. **distills keywords** from the feature (`spec.md` title, domain nouns, entity names,
   FR terms, branch slug) — seeded with any pinned `keywords:` — and **clusters them
   into thematic groups** (capped by `max_keyword_groups` / `max_keywords_per_group`),
4. **prints the keyword groups**, then runs **one recall pass per group** against each
   configured memory tool (a tool with a pinned `query:` bypasses the groups),
5. merges results into a capped *Prior art* summary and hands it, with the keyword
   groups, to the stage.

### Keywords

`keyword_source` controls where terms come from: `auto` (extracted from the feature),
`config` (only your pinned `keywords:`), or `auto+config` (both — default). More groups
means broader coverage at the cost of more recall passes.

## Uninstall

```bash
specify extension remove memory
```

## License

MIT © Andrey Zaytsev
