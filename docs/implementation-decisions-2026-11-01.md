# Arrmes implementation decisions

Arrmes needs trustworthy service workflows and a portable runtime before a larger tool count is useful. This increment preserves the released neural contract and adds bounded native reads through a separate, explicit route. It also removes two live integration claims that the code could not substantiate.

## Preserve the neural baseline

The released Qwen adapter learned `arrmes.tools.v2`: 18 service actions and two control actions. The v2 schema, prompt tool list and contract hash remain unchanged. Native service reads use one typed `service_query` extension with a checked service/operation matrix, reported as `explicit_service_deterministic` in responses. This gives useful new natural-language workflows without claiming that an unchanged model learned new operations. Its grammar requires an explicit service and quoted text for title-bearing lookup/subtitle queries; unclear requests abstain.

Retraining the small model is deferred until the new native workflows and an independent evaluation split are stable. Frozen acceptance inputs must never become training material. The fresh benchmark also records its one known normalized-template overlap rather than silently replacing a case after inference.

## Use native APIs for new reads

Native adapters take API roots and credentials only from an operator file. They check finite page/record/response budgets, reject redirects, sanitize observations and enforce service-specific scopes before network access. Search means metadata lookup; the read path does not issue an indexer-search command. Bazarr queries require an explicit movie or TV scope and exact entity resolution. qBittorrent uses its native WebAPI rather than the legacy QUI aggregation endpoint.

The existing `seer_discover` name denotes request-portal discovery and does not prove a native Seerr connection. The new `seerr` adapter implements only documented native health and metadata search. Request management awaits a verified caller identity mapping. Live Armiger parsing previously combined a local heuristic with an image-generation catalog request, and Waypoint status queried a Commskip endpoint. Both now report unsupported. Their old sandbox fixtures remain reproducible and do not establish live support.

## Keep compounds bounded

The runtime accepts at most four independent explicit native reads, such as “Show Sonarr queue and Radarr history.” It validates every clause and permission before the first dispatch. A write or ambiguous clause stops the whole plan. Once an all-read plan starts, an unavailable service yields a partial result while the other independent reads continue. Conditional plans and compound writes remain unsupported.

## Bind confirmation to the resolved target

Confirmation tokens are single use and bound to the principal, canonical arguments, resolved entity and expiry. At redemption, the executor rechecks the caller's current scopes. Immediately before a live mutation, the write handler compares the newly resolved ID with the confirmed ID. A removed and re-added series or changed metadata result requires a new confirmation. A denial also consumes the token, preventing a stale preview from being replayed later. The entire legacy adapter call is serialized because its transport guard holds mutable per-call effect state.

Execution references come from the server's verified per-principal ledger. A legacy HTTP client may send `context`, but the execution endpoint ignores it; the non-executing route preview can still use caller context.

## Delegate inference to an optional native runtime

The portable path uses a native llama.cpp server through the Python standard library. CPU installs require neither CUDA nor MLX. MLX remains optional on Apple Silicon. Conversion must fuse the exact chosen adapter, verify all adapted matrix deltas, preserve tokenizer/chat-template behavior, and evaluate the resulting GGUF. A successful conversion or compilation is not a behavior-parity result. Platform claims and measured memory belong beside their hardware and backend receipts in the runtime and benchmark reports.

The public HF adapter and current internal v6 checkpoint differ materially. A conversion of the internal checkpoint is a different model from the published release, even if both directories contain “v6.” Neither file size nor an MLX allocation counter is a substitute for measured process RSS or discrete GPU memory.
