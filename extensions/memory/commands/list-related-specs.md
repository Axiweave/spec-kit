---
description: "Recall related specs from memory and write a '## Related specs' section into research.md"
---

# List Related Specs

Recall the specs related to the current feature (based on memory) and write them
into the feature's `research.md` as a compact list. This is the **writing** companion
to `speckit.memory.recall`: it runs on `after_plan` — once Phase 0 has produced
`research.md` — and edits exactly one section of that file. It touches nothing else.

## User Input

$ARGUMENTS

## Behavior

1. **Determine the event** (typically `after_plan`) and the current feature from the
   branch name. Resolve the research file at `specs/<NNN-slug>/research.md`.
2. **Load config** from `.specify/extensions/memory/memory-config.yml`
   (fall back to manifest `config.defaults`).
3. **Enabled gate**: if `settings.related_specs_enabled` is false, skip with one line.
4. **Research file gate**: if `research.md` does not exist, skip with a note
   (do not create a stray file). Honours `settings.fail_open`.
5. **Prepare keyword groups** exactly as `recall` does: distill terms from the
   `spec.md` title, domain nouns, entity names, FR terms, and branch slug — seeded
   with any pinned `keywords:`, per `keyword_source` — then cluster into groups
   (capped by `max_keyword_groups` / `max_keywords_per_group`).
6. **Recall per group**: invoke each tool in `memory_tools` once per keyword group
   (a tool with a pinned `query:` runs once with that query instead).
7. **Extract specs**: from the merged results keep only entries that identify a
   **spec** — i.e. a `specs/<NNN>-<slug>` path or an `<NNN>` spec id with a slug/title.
   - Dedupe by spec id.
   - **Exclude the current feature's own spec.**
   - Sort by spec id ascending; cap at `settings.related_specs_max`.
8. **Write the section** into `research.md`, idempotently:
   - Section heading: `## {settings.related_specs_heading}`.
   - If the section already exists, **replace its body**; otherwise **append** the
     section to the end of the file.
   - One entry per line, **id + title only**:
     ```
     ## Related specs (from memory)

     - [092] humanize-li-access
     - [094] gateway-walkers
     ```
   - If no related specs are found, write the heading with a single line
     `- _none found_` (so the section is present and the absence is explicit).
9. **Report** the research.md path and the count written. Modify no other file.

## Configuration

In `.specify/extensions/memory/memory-config.yml` (`memory_tools`, `keywords`,
`keyword_source`, `max_keyword_groups`, `max_keywords_per_group` are shared with
`recall`):

```yaml
settings:
  related_specs_enabled: true
  related_specs_heading: "Related specs (from memory)"
  related_specs_max: 20
```

## Graceful Degradation

- **Disabled / no research.md / no tools** → skip with a one-line message; never error.
- **A tool errors or a group returns nothing** → with `settings.fail_open: true`
  (default), continue; write whatever specs were found (or `- _none found_`).
- Side-effect scope is exactly one section of one file (`research.md`); the command
  is idempotent and safe to re-run.
