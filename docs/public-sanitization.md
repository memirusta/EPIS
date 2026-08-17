# Public Sanitization Notes

This public package was built from a private working EPIS snapshot. The following categories were intentionally removed rather than merely hidden:

- API keys, encryption keys, credentials, and environment files
- WhatsApp browser/session authentication state
- private messages and conversation/thinking logs
- people/relationship records
- personal identity autobiography/profile data
- location, phone/screen activity, biometrics, and Gadgetbridge databases
- logs, backups, cache files, temporary diagnostics, and `node_modules`
- the private Master Design Document, which contains personal deployment details

Safe synthetic templates are included in `examples/`.
