# Arrmes comparative evaluation

The original public adapter and source scored 63/80 exact raw calls on newly authored diagnostic requests, rising to 68/80 after verification. The same underlying untuned Qwen3 0.6B scored 20/80 raw and 37/80 verified. These 80 requests were frozen before inference; they include one subsequently identified normalized-template overlap and are not a clean acceptance claim.

The stronger Qwen3 4B Instruct reference scored 54/80 raw under the same tool information and policy on the same Mac. Qwen3 1.7B frequently returned bare JSON rather than the required native wrapper, so native-format and translated semantic results are reported separately. Granite results depend materially on precision and formatting; the model-family result must not be inferred from a failed community quantization.

The exact published adapter converted to F16 and Q4 GGUF retains useful accuracy over these tasks. Individual predictions differ across backend, precision and CPU quantizer. Model file size, process RSS, allocator memory and VRAM are separate measurements.

The versioned harness and inputs are included in `scripts/benchmark_comparison.py` and `data/overnight-v1/`. Detailed raw receipts and the complete comparison report are being integrated in this development branch before review completion. This is not a release or deployment claim.
