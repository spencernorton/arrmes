# Arrmes development

GitHub is the product source. Keep model weights, credentials and private deployment configuration out of Git. Follow CONTRIBUTING.md, SECURITY.md and the runtime guide.

- Run `python -m pytest -q`; core tests need no model or accelerator.
- Keep the released v2 neural tool schemas and hash stable when adding native v3 routes.
- Never train or tune on versioned acceptance or benchmark inputs. Record model revisions, exact artifacts, backend versions and raw outputs.
- Separate model accuracy, format translation, verifier corrections and fixture execution in every report.
- Service operations require typed arguments, exact entity resolution, scoped identity and finite resource budgets. Consequential writes require single-use confirmation.
- Test only against isolated fixtures during training/evaluation; production data and services are excluded.
- Platform compilation, fixture tests and actual inference are separate evidence. State precisely which was run.
- Append meaningful development entries at the top of AGENTS/journal.md.
