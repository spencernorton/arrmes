# Security Policy

## Reporting a Vulnerability

Report security vulnerabilities privately through [GitHub Security Advisories](https://github.com/spencernorton/arrmes/security/advisories/new). Do not open a public issue for suspected vulnerabilities. There is no e-mail address; that is deliberate. Reports receive an acknowledgement within seven days. Only the latest tagged release is supported.

## Trust Model

- **Model outputs are untrusted proposals.** The language model never receives service API keys, cookies, or bearer tokens, and its output is parsed strictly as data (`arrmes.protocol.decode`).
- **Pre-dispatch entity grounding.** Every proposed tool call is checked by `arrmes.planner.verify_proposal` to confirm that target titles, artists, albums, and release strings originate in the caller utterance or the caller's verified `SessionEntityLedger`.
- **Read-only transport invariant.** Native service reads use fixed HTTP `GET` endpoints. The legacy Bitmagnet facade uses a fixed read-only GraphQL query over `POST`; arbitrary GraphQL and mutation text are not accepted. Audit logs redact query-string credentials, and returned projections omit private fields.
- **Cryptographic write confirmation.** Consequential writes require a short-lived, single-use HMAC-SHA256 token bound to the caller, canonical arguments, resolved entity and expiry. Redemption rechecks current scopes, the write gate and the resolved target. Expired, changed and replayed confirmations are refused. The HTTP entry point rejects direct write mode.
- **Trusted execution context.** Execution endpoints use the per-caller verified entity ledger and ignore client-supplied execution context. Credentials and service origins come from operator-owned registries, never model output. Registries are loaded at startup; restart after changes.
- **Finite dispatch.** Service queries, response sizes, pagination, independent read compounds, confirmations and inference output are bounded. Native writes are not automatically retried. Unavailable services and unresolved targets produce errors or clarification rather than another service's result.
