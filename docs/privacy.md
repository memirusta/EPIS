# Privacy Model

EPIS is designed around local-first personal data handling.

- Secrets and encryption keys are never committed.
- Raw memory, behavioral logs, message history, location, biometrics, and browser/WhatsApp sessions are excluded from the public repository.
- The Layer 2 privacy module can replace known names and places with local tokens before sending a task to an external model, then reverse the mapping locally.
- Monitoring integrations are opt-in and disabled in the public defaults.
- A deployment should expose network services only deliberately and protect them with authentication.

This repository contains architecture and source code, not a personal dataset.
