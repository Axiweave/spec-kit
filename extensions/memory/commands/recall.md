---
description: "Recall prior specs/decisions relevant to the current feature via the configured memory tools"
---

# Memory Recall

Recall prior specs, sibling features, and past decisions relevant to the current
feature — so a stage (e.g. writing the spec, or Phase 0 research) starts from what
the project already knows instead of a blank page.

This command is invoked as a hook (typically `before_specify` / `before_plan`). It is
**read-only**: it recalls memory and summarizes it; it never writes specs, plans, or code.

## User Input

$ARGUMENTS

## Behavior

1. **Determine the event** from the hook context that triggered this command
   (e.g. `before_specify`, `before_plan`).
2. **Load config** from `.specify/extensions/memory/memory-config.yml`.
   - If the file is missing, fall back to the manifest `config.defaults` and a
     single `memsearch:memory-recall` tool.
3. **Stage gate**: look up `stages.<event>`; if absent use `stages.default`.
   If the stage is disabled, **skip** (print one line saying so) and return.
4. **Gather candidate terms** for the current feature, per `settings.keyword_source`
   (`auto` | `config` | `auto+config`):
   - `config` → the pinned `keywords:` list (if any).
   - `auto` → salient terms extracted from the feature: the `spec.md` title, core
     domain nouns, entity/model names, notable functional-requirement terms, and
     branch-slug tokens.
   - `auto+config` (default) → both, with pinned `keywords:` always included.
5. **Form keyword groups**: cluster the candidate terms into coherent thematic
   groups (each group = one facet of the feature, e.g. `[auth, session, token]`,
   `[onboarding, banner]`). Cap at `settings.max_keyword_groups` groups and
   `settings.max_keywords_per_group` terms per group; drop the least-salient overflow.
6. **Print the keyword groups** that will be searched, e.g.:

   ```
   Memory recall — keyword groups (before_specify):
     1. authentication, session, token
     2. onboarding, banner, install
   ```

7. **Search once per group**: for each keyword group, invoke each tool in
   `memory_tools`, passing the group's terms as the query.
   - A tool that pins its own `query` ignores the groups and runs **once** with that
     fixed query instead.
8. **Merge & summarize**: consolidate all results (across groups and tools) into one
   short **Prior art** list — spec IDs / titles + a one-line takeaway each, deduped,
   capped at `settings.max_findings`.
9. **Hand off**: surface the keyword groups and the Prior-art summary so the calling
   stage can fold them in. Do not modify any artifact.

## Configuration

In `.specify/extensions/memory/memory-config.yml`:

```yaml
memory_tools:
  - command: "memsearch:memory-recall"
    description: "Semantic recall over past sessions, specs and decisions"
    # query: "<fixed query; bypasses keyword groups for this tool>"
  # - command: "some-plugin:notes-search"

keywords:                 # optional seed terms, always included (config / auto+config)
  # - "authentication"
  # - "session token"

stages:
  default: true
  before_specify: true
  before_plan: true

settings:
  read_only: true
  fail_open: true
  max_findings: 8
  keyword_source: "auto+config"   # auto | config | auto+config
  max_keyword_groups: 4           # how many groups → how many recall passes
  max_keywords_per_group: 5
```

## Graceful Degradation

- **No tools configured / config missing** → use the `memsearch:memory-recall`
  default; if that is unavailable, say so and continue.
- **No candidate terms** → fall back to a single search on the raw feature topic.
- **A tool errors or a group returns nothing** → with `settings.fail_open: true`
  (default), note it and continue; never block the stage.
- **Stage disabled in config** → skip silently with a one-line message.
- Always non-blocking and side-effect free.
