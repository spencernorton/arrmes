# Contributing

## How changes land

Pull requests are reviewed and squash-merged here, with credit in the release notes. Sign off every commit with `git commit -s` (Developer Certificate of Origin); no CLA is required.

## Verification before submitting

Run the full test suite and ensure zero regressions:

```sh
.venv/bin/pytest -q
```

## Out of scope

- Arbitrary shell execution, container restarts, media deletion, or bulk torrent deletion actions (`unsupported(reason="unsafe")` by design).
- Cloud-hosted inference dependencies in the core routing path.
- Hardcoded benchmark entity lists or split-specific rules inside `src/arrmes/`.
