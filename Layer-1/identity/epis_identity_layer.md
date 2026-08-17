# EPIS Identity Layer — Public Template

EPIS keeps learned identity and unresolved questions outside the base model.

## Learning flow
1. Detect uncertainty or a new concept.
2. Record unresolved items in `pending.json`.
3. Research, ask the user, or combine both.
4. Store approved conclusions in `epis_self.json`.

## Drift protection
A scheduled drift check compares recent learned conclusions with the personality/value files. Material differences should be surfaced to the user instead of silently changing the system's character.

This file is a public template. Personal conclusions and private identity data are intentionally excluded from the repository.
