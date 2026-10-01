---
description: Create a specification and store it in spec.md.
---

## User Input

```text
$ARGUMENTS
```

## Outline

1. **Ask the user** for the feature directory path (e.g., `specs/my-feature`). Do not proceed until provided.

2. Resolve the path against `workspace_root` from `specify project info --json`, then create the directory.
   Read `feature_selection` from the workspace's `.specify/init-options.json`. Its default is `context`.
   In `context` mode, keep saved selection unchanged and carry this path in conversation context.
   In `automatic` mode, use the installed common helper to persist this feature.
   Pass `SPECIFY_FEATURE_DIRECTORY` only for that invocation. Honor `SPECIFY_FEATURE_NO_PERSIST`.

3. Create a specification from the user input and store it in `<feature_directory>/spec.md`.
   - Overview, functional requirements, user scenarios, success criteria
   - Every requirement must be testable
   - Make informed defaults for unspecified details
