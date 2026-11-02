# Security Policy

## Reporting a Vulnerability

Report security vulnerabilities privately through [GitHub Security Advisories](https://github.com/spencernorton/arrmes/security/advisories/new). Do not open a public issue for suspected vulnerabilities. There is no e-mail address; that is deliberate. Reports receive an acknowledgement within seven days. Only the latest tagged release is supported.

## Trust Model

- **Model outputs are untrusted proposals.** The language model never receives service API keys, cookies, or bearer tokens, and its output is parsed strictly as data (`arrmes.protocol.decode`).
- **Pre-dispatch entity grounding.** Every proposed tool call is checked by `arrmes.planner.verify_proposal` to confirm that target titles, artists, albums, and release strings originate in the caller utterance or the caller's verified `SessionEntityLedger`.
- **Read-only transport invariant.** Read-effect operations are restricted to HTTP `GET` requests (plus stateless release parsing) and automatically redact query-string tokens from audit logs.
- **Cryptographic write confirmation.** In `confirm` mode (the default), write-effect operations never mutate upstream services directly; they mint a short-lived, single-use HMAC-SHA256 confirmation token bound to the caller principal and canonical arguments.
