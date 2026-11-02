---
license: apache-2.0
base_model: Qwen/Qwen3-0.6B
language:
  - en
tags:
  - tool-calling
  - function-calling
  - mlx
  - gguf
  - arrmes
---

# Arrmes Qwen3-0.6B (`arrmes-qwen3-0.6b-v7`)

Fine-tuned `Qwen/Qwen3-0.6B` checkpoint for the frozen 20-tool `arrmes.tools.v2` Hermes `<tool_call>` contract.

## Scope & Limitations

- Targets the 20-action `arrmes.tools.v2` contract (`e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8`).
- Expanded `arrmes.tools.v3` operations (`service_query`, `bazarr_subtitle_search`, `bazarr_subtitle_download`) are handled by explicit grammar routing or `ArrmesNeuralRouterV3` rather than generative `<tool_call>` token emission.
- Downstream pre-execution verification (`verify_proposal`) and confirmation tokens (`WriteConfirmationStore`) remain mandatory before any state-changing operation executes.
