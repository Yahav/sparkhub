# Benchmark summary

The benchmark SparkHub's choice of models and settings is based on: candidate models measured on one DGX Spark in
October 2026. It ran from a separate, unpublished project; the `scripts/`, `results/` and `writing/` paths below
refer to that project.

<!-- BEGIN AUTO (scripts/summarize.py) -->

## S0 — Speed

Random tokens: `vllm bench serve`, 2000 in / 500 out (16000 in for the 16k columns), `--ignore-eos`. Real prompt: one 1200-token answer, thinking off, tok/s incl. prefill. Per-stream = total ÷ concurrency.

| Model | Load s | Mem GiB | c1 tok/s (random) | c1 tok/s (real) | c2 total | c2 /stream | c4 total | c4 /stream | TTFT p50 2k ms | TTFT p50 16k ms | c4@16k total | c4@16k TTFT ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36 | 280 | 73.1 | 109.0 | 105.1 | 152.1 | 76.0 | 221.2 | 55.3 | 283 | 2267 | 137.8 | 2940 |
| qwen36-fp8 | 476 | 78.6 | 48.6 | 57.1 | 84.4 | 42.2 | 111.6 | 27.9 | 742 | 2518 | 89.5 | 3518 |
| coder-next | 381 | 77.5 | 36.8 | 37.7 | 68.2 | 34.1 | 107.3 | 26.8 | 484 | 2687 | 75.9 | 4050 |
| coder-next-fp8 | 485 | 99.1 | 38.3 | 40.7 | 55.4 | 27.7 | 72.6 | 18.2 | 988 | 3306 | 55.7 | 4840 |
| flash | 220 | 101.6 | 27.9 | 42.8 | 40.5 | 20.3 | 61.6 | 15.4 | 1014 | 6799 | 39.4 | 7232 |
| nemotron-super | 686 | 98.8 | 23.1 | 27.0 | 36.4 | 18.2 | 51.6 | 12.9 | 1386 | 7827 | 34.1 | 8411 |
| devstral-small | 275 | 71.2 | 9.0 | 9.1 | 18.1 | 9.1 | 35.3 | 8.8 | 636 | 5091 | 25.8 | 5473 |
| flash-fp4l | 917 | 112.7 | 33.5 | 47.0 | 50.3 | 25.2 | 64.6 | 16.2 | 1004 | 6307 | 43.4 | 7532 |
| nemotron-super-fix | 656 | 99.8 | – | – | – | – | – | – | – | – | – | – |

Smoke checks (scripts/smoke.sh):

| Model | Tool call | Truncation | Prefix cache (1st→2nd s, cached tokens) |
|---|---|---|---|
| qwen36 | OK | OK | OK (1.38→0.65, 6432) |
| coder-next | OK | OK | OK (1.73→0.27, 9648) |
| devstral-small | OK | OK | OK (3.7→0.46, 10512) |
| nemotron-super | OK | OK | OK (5.1→3.04, 4336) |
| flash | OK | OK | OK (3.76→1.0, 8000) |
| qwen36-fp8 | OK | OK | OK (1.54→0.74, 6432) |
| coder-next-fp8 | OK | OK | OK (2.01→0.4, 9648) |
| nemotron-super-fix | OK | OK | OK (5.12→3.05, 4336) |
| flash-fp4l | OK | OK | OK (4.4→0.35, None) |

## S1 — Mini agent suite (8 Python tasks, opencode v2 on the host)

| Model | Passed /8 per pass | Avg | Tool calls | Tool errors | Malformed | Median s | Total s | Timeouts | Contaminated |
|---|---|---|---|---|---|---|---|---|---|
| qwen36 | 7 / 7 | 7 | 124 | 10 | 0 | 20 | 382 | 0 | 0 |
| qwen36-fp8 | 8 / 8 | 8 | 91 | 0 | 0 | 31 | 713 | 0 | 0 |
| coder-next | 6 / 8 | 7 | 66 | 0 | 0 | 19 | 397 | 0 | 0 |
| coder-next-fp8 | 7 / 8 | 7.5 | 78 | 5 | 0 | 24 | 421 | 0 | 0 |
| flash | 8 / 8 | 8 | 91 | 2 | 0 | 62 | 1067 | 0 | 0 |
| nemotron-super | 8 / 8 | 8 | 159 | 4 | 0 | 120 | 2789 | 0 | 0 |
| devstral-small | 7 / 8 | 7.5 | 246 | 2 | 0 | 243 | 7189 | 1 | 0 |
| flash-fp4l | 8 / 8 | 8 | 147 | 8 | 0 | 78 | 1657 | 0 | 0 |

## S2 — Technical knowledge + instruction following (lm-eval)

Bold = rescored with the format-tolerant extractor (scripts/lmeval_rescore.py); stock = lm-eval's own filter, which misses \boxed{}, "Answer: C", markdown, etc. Thinking on. IFEval as scored by lm-eval.

| Task | qwen36 | qwen36-fp8 | coder-next | coder-next-fp8 | flash | nemotron-super | devstral-small |
|---|---|---|---|---|---|---|---|
| mmlu_college_computer_science_generative | **96.0%** (0.0% stock, n=100, no-ans 1) | **94.0%** (1.0% stock, n=100, no-ans 2) | **96.0%** (0.0% stock, n=100, no-ans 1) | **98.0%** (0.0% stock, n=100, no-ans 0) | **69.0%** (5.0% stock, n=100, no-ans 28) | **95.0%** (46.0% stock, n=100, no-ans 4) | **79.0%** (0.0% stock, n=100, no-ans 6) |
| mmlu_computer_security_generative | **87.0%** (0.0% stock, n=100, no-ans 0) | **86.0%** (0.0% stock, n=100, no-ans 3) | **76.0%** (0.0% stock, n=100, no-ans 9) | **86.0%** (0.0% stock, n=100, no-ans 3) | **85.0%** (2.0% stock, n=100, no-ans 8) | **92.0%** (45.0% stock, n=100, no-ans 0) | **76.0%** (0.0% stock, n=100, no-ans 4) |
| mmlu_pro_computer_science | **89.0%** (89.0% stock, n=200, no-ans 0) | **87.5%** (87.5% stock, n=200, no-ans 0) | **85.5%** (84.5% stock, n=200, no-ans 0) | **84.5%** (84.0% stock, n=200, no-ans 0) | **87.5%** (87.5% stock, n=200, no-ans 7) | **88.0%** (87.0% stock, n=200, no-ans 1) | **73.0%** (57.0% stock, n=200, no-ans 1) |
| mmlu_pro_engineering | **80.0%** (80.0% stock, n=200, no-ans 0) | **82.5%** (82.5% stock, n=200, no-ans 0) | **74.5%** (75.5% stock, n=200, no-ans 2) | **78.5%** (79.0% stock, n=200, no-ans 1) | **75.5%** (75.5% stock, n=200, no-ans 39) | **73.0%** (72.5% stock, n=200, no-ans 14) | **44.0%** (42.0% stock, n=200, no-ans 5) |
| ifeval (prompt strict / loose) | 90.5% / 93.5% | 88.0% / 92.0% | 78.0% / 81.0% | 82.0% / 85.0% | 91.5% / 94.5% | 90.0% / 92.0% | 65.5% / 70.0% |

## S6 — Reasoning (thinking on)

BBH CoT (limit 50 per subtask) + GPQA diamond (198). Rescored as S2.

| Task | qwen36 | qwen36-fp8 | coder-next | coder-next-fp8 | flash | nemotron-super | devstral-small |
|---|---|---|---|---|---|---|---|
| bbh_cot_fewshot_boolean_expressions | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (90.0% stock, n=50, no-ans 0) | **100.0%** (4.0% stock, n=50, no-ans 0) | **98.0%** (0.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_causal_judgement | **70.0%** (70.0% stock, n=50, no-ans 0) | **68.0%** (68.0% stock, n=50, no-ans 0) | **62.0%** (0.0% stock, n=50, no-ans 0) | **68.0%** (0.0% stock, n=50, no-ans 0) | **70.0%** (66.0% stock, n=50, no-ans 2) | **66.0%** (0.0% stock, n=50, no-ans 0) | **60.0%** (48.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_date_understanding | **92.0%** (92.0% stock, n=50, no-ans 0) | **92.0%** (92.0% stock, n=50, no-ans 0) | **92.0%** (2.0% stock, n=50, no-ans 0) | **92.0%** (0.0% stock, n=50, no-ans 0) | **96.0%** (90.0% stock, n=50, no-ans 0) | **92.0%** (14.0% stock, n=50, no-ans 0) | **84.0%** (72.0% stock, n=50, no-ans 3) |
| bbh_cot_fewshot_disambiguation_qa | **78.0%** (78.0% stock, n=50, no-ans 0) | **82.0%** (82.0% stock, n=50, no-ans 0) | **70.0%** (0.0% stock, n=50, no-ans 0) | **72.0%** (0.0% stock, n=50, no-ans 0) | **76.0%** (64.0% stock, n=50, no-ans 1) | **80.0%** (0.0% stock, n=50, no-ans 0) | **74.0%** (56.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_dyck_languages | **92.0%** (92.0% stock, n=50, no-ans 3) | **92.0%** (90.0% stock, n=50, no-ans 1) | **88.0%** (0.0% stock, n=50, no-ans 0) | **84.0%** (0.0% stock, n=50, no-ans 0) | **92.0%** (82.0% stock, n=50, no-ans 2) | **78.0%** (0.0% stock, n=50, no-ans 7) | **54.0%** (20.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_formal_fallacies | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (12.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **74.0%** (34.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_geometric_shapes | **78.0%** (78.0% stock, n=50, no-ans 0) | **78.0%** (78.0% stock, n=50, no-ans 0) | **84.0%** (0.0% stock, n=50, no-ans 0) | **82.0%** (0.0% stock, n=50, no-ans 0) | **78.0%** (58.0% stock, n=50, no-ans 0) | **78.0%** (18.0% stock, n=50, no-ans 0) | **58.0%** (38.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_hyperbaton | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **96.0%** (0.0% stock, n=50, no-ans 2) | **100.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (16.0% stock, n=50, no-ans 0) | **100.0%** (90.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_logical_deduction_five_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (32.0% stock, n=50, no-ans 0) | **92.0%** (32.0% stock, n=50, no-ans 1) |
| bbh_cot_fewshot_logical_deduction_seven_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (66.0% stock, n=50, no-ans 0) | **90.0%** (12.0% stock, n=50, no-ans 5) | **82.0%** (16.0% stock, n=50, no-ans 4) |
| bbh_cot_fewshot_logical_deduction_three_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (2.0% stock, n=50, no-ans 0) | **100.0%** (2.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (60.0% stock, n=50, no-ans 0) | **94.0%** (94.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_movie_recommendation | **96.0%** (96.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **94.0%** (0.0% stock, n=50, no-ans 0) | **90.0%** (0.0% stock, n=50, no-ans 0) | **92.0%** (48.0% stock, n=50, no-ans 2) | **90.0%** (24.0% stock, n=50, no-ans 0) | **78.0%** (32.0% stock, n=50, no-ans 1) |
| bbh_cot_fewshot_multistep_arithmetic_two | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **96.0%** (0.0% stock, n=50, no-ans 0) | **88.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **94.0%** (0.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_navigate | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (80.0% stock, n=50, no-ans 0) | **100.0%** (2.0% stock, n=50, no-ans 0) | **94.0%** (82.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_object_counting | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **82.0%** (0.0% stock, n=50, no-ans 0) | **70.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (40.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_penguins_in_a_table | **98.0%** (98.0% stock, n=50, no-ans 1) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) | **98.0%** (38.0% stock, n=50, no-ans 0) | **90.0%** (78.0% stock, n=50, no-ans 1) |
| bbh_cot_fewshot_reasoning_about_colored_objects | **100.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **98.0%** (86.0% stock, n=50, no-ans 0) | **98.0%** (16.0% stock, n=50, no-ans 0) | **98.0%** (80.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_ruin_names | **80.0%** (80.0% stock, n=50, no-ans 0) | **80.0%** (80.0% stock, n=50, no-ans 2) | **86.0%** (0.0% stock, n=50, no-ans 0) | **84.0%** (0.0% stock, n=50, no-ans 1) | **92.0%** (72.0% stock, n=50, no-ans 0) | **84.0%** (10.0% stock, n=50, no-ans 0) | **74.0%** (62.0% stock, n=50, no-ans 2) |
| bbh_cot_fewshot_salient_translation_error_detection | **76.0%** (76.0% stock, n=50, no-ans 0) | **78.0%** (78.0% stock, n=50, no-ans 0) | **82.0%** (0.0% stock, n=50, no-ans 0) | **76.0%** (0.0% stock, n=50, no-ans 0) | **78.0%** (72.0% stock, n=50, no-ans 1) | **86.0%** (0.0% stock, n=50, no-ans 0) | **64.0%** (48.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_snarks | **90.0%** (90.0% stock, n=50, no-ans 0) | **90.0%** (90.0% stock, n=50, no-ans 0) | **90.0%** (0.0% stock, n=50, no-ans 0) | **86.0%** (0.0% stock, n=50, no-ans 0) | **92.0%** (74.0% stock, n=50, no-ans 0) | **92.0%** (0.0% stock, n=50, no-ans 0) | **90.0%** (68.0% stock, n=50, no-ans 1) |
| bbh_cot_fewshot_sports_understanding | **96.0%** (96.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **74.0%** (0.0% stock, n=50, no-ans 0) | **68.0%** (0.0% stock, n=50, no-ans 0) | **90.0%** (90.0% stock, n=50, no-ans 0) | **76.0%** (4.0% stock, n=50, no-ans 0) | **96.0%** (96.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_temporal_sequences | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (20.0% stock, n=50, no-ans 0) | **100.0%** (98.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_five_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **98.0%** (0.0% stock, n=50, no-ans 0) | **98.0%** (94.0% stock, n=50, no-ans 0) | **100.0%** (22.0% stock, n=50, no-ans 0) | **100.0%** (82.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_seven_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **98.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (76.0% stock, n=50, no-ans 0) | **98.0%** (16.0% stock, n=50, no-ans 0) | **98.0%** (32.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_three_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (82.0% stock, n=50, no-ans 0) | **100.0%** (98.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_web_of_lies | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (0.0% stock, n=50, no-ans 0) | **100.0%** (96.0% stock, n=50, no-ans 0) | **100.0%** (6.0% stock, n=50, no-ans 0) | **100.0%** (98.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_word_sorting | **98.0%** (98.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **82.0%** (0.0% stock, n=50, no-ans 0) | **86.0%** (0.0% stock, n=50, no-ans 0) | **98.0%** (72.0% stock, n=50, no-ans 0) | **90.0%** (4.0% stock, n=50, no-ans 1) | **64.0%** (50.0% stock, n=50, no-ans 0) |
| gpqa_diamond_cot_zeroshot | **80.8%** (0.0% stock, n=198, no-ans 8) | **76.3%** (0.0% stock, n=198, no-ans 16) | **75.8%** (0.0% stock, n=198, no-ans 3) | **73.2%** (0.0% stock, n=198, no-ans 1) | **65.7%** (0.0% stock, n=198, no-ans 60) | **64.1%** (0.0% stock, n=198, no-ans 58) | **52.5%** (0.0% stock, n=198, no-ans 11) |

## S6 — Reasoning (thinking off)

Same tasks with chat_template_kwargs enable_thinking=false.

| Task | qwen36 | qwen36-fp8 | flash | nemotron-super |
|---|---|---|---|---|
| bbh_cot_fewshot_boolean_expressions | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_causal_judgement | **54.0%** (54.0% stock, n=50, no-ans 0) | **66.0%** (64.0% stock, n=50, no-ans 0) | **66.0%** (64.0% stock, n=50, no-ans 0) | **66.0%** (66.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_date_understanding | **92.0%** (92.0% stock, n=50, no-ans 0) | **92.0%** (90.0% stock, n=50, no-ans 0) | **96.0%** (88.0% stock, n=50, no-ans 0) | **94.0%** (92.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_disambiguation_qa | **86.0%** (84.0% stock, n=50, no-ans 0) | **90.0%** (88.0% stock, n=50, no-ans 0) | **82.0%** (82.0% stock, n=50, no-ans 0) | **80.0%** (80.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_dyck_languages | **94.0%** (86.0% stock, n=50, no-ans 0) | **94.0%** (88.0% stock, n=50, no-ans 0) | **96.0%** (94.0% stock, n=50, no-ans 0) | **76.0%** (64.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_formal_fallacies | **100.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (94.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_geometric_shapes | **80.0%** (80.0% stock, n=50, no-ans 0) | **76.0%** (76.0% stock, n=50, no-ans 0) | **80.0%** (80.0% stock, n=50, no-ans 0) | **54.0%** (52.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_hyperbaton | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_logical_deduction_five_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (96.0% stock, n=50, no-ans 0) | **100.0%** (96.0% stock, n=50, no-ans 0) | **96.0%** (78.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_logical_deduction_seven_objects | **98.0%** (96.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) | **88.0%** (54.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_logical_deduction_three_objects | **98.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **96.0%** (96.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_movie_recommendation | **94.0%** (92.0% stock, n=50, no-ans 0) | **96.0%** (96.0% stock, n=50, no-ans 0) | **94.0%** (94.0% stock, n=50, no-ans 0) | **92.0%** (92.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_multistep_arithmetic_two | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_navigate | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **92.0%** (92.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_object_counting | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_penguins_in_a_table | **100.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **96.0%** (96.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_reasoning_about_colored_objects | **100.0%** (94.0% stock, n=50, no-ans 0) | **100.0%** (94.0% stock, n=50, no-ans 0) | **98.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (92.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_ruin_names | **86.0%** (82.0% stock, n=50, no-ans 0) | **86.0%** (78.0% stock, n=50, no-ans 0) | **90.0%** (82.0% stock, n=50, no-ans 0) | **86.0%** (82.0% stock, n=50, no-ans 1) |
| bbh_cot_fewshot_salient_translation_error_detection | **78.0%** (72.0% stock, n=50, no-ans 0) | **72.0%** (72.0% stock, n=50, no-ans 0) | **70.0%** (68.0% stock, n=50, no-ans 0) | **72.0%** (72.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_snarks | **90.0%** (90.0% stock, n=50, no-ans 0) | **88.0%** (88.0% stock, n=50, no-ans 0) | **90.0%** (88.0% stock, n=50, no-ans 0) | **88.0%** (88.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_sports_understanding | **92.0%** (92.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **92.0%** (92.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_temporal_sequences | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_five_objects | **98.0%** (98.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_seven_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (90.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_tracking_shuffled_objects_three_objects | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_web_of_lies | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) |
| bbh_cot_fewshot_word_sorting | **100.0%** (98.0% stock, n=50, no-ans 0) | **98.0%** (98.0% stock, n=50, no-ans 0) | **100.0%** (100.0% stock, n=50, no-ans 0) | **98.0%** (78.0% stock, n=50, no-ans 0) |
| gpqa_diamond_cot_zeroshot | **78.3%** (0.0% stock, n=198, no-ans 4) | **78.8%** (0.0% stock, n=198, no-ans 3) | **83.3%** (0.5% stock, n=198, no-ans 2) | **63.1%** (2.0% stock, n=198, no-ans 49) |

## S3 — Terminal-Bench 2.0 (technical subset, arm64: 53 tasks)

opencode 1.18.35 in each task container, 4 trials in parallel. Silent stops = failed trials whose last turn ended after reasoning only (no tool call, no text): the model quit early instead of failing at the work.

| Model | Solved | Rate | Exceptions | Median agent s | Agent hours | Tool calls | Tool errors | Silent stops (failed) | Tokens in / out |
|---|---|---|---|---|---|---|---|---|---|
| qwen36 | 20/53 | 37.7% | 12 | 509 | 10.8 | 1754 | 11 | 0 | 58.4M / 0.42M |
| qwen36-fp8 | 17/53 | 32.1% | 18 | 528 | 10.8 | 1174 | 3 | 0 | 25.0M / 0.18M |
| coder-next | 16/53 | 30.2% | 24 | 389 | 12.1 | 2921 | 50 | 0 | 103.6M / 0.68M |
| coder-next-fp8 | 13/53 | 24.5% | 25 | 322 | 11.3 | 2042 | 46 | 1 | 52.5M / 0.52M |
| flash | 31/53 | 58.5% | 17 | 754 | 14.2 | 1049 | 2 | 1 | 26.4M / 0.18M |
| nemotron-super | 11/53 | 20.8% | 19 | 826 | 13.1 | 978 | 4 | 17 | 18.7M / 0.43M |
| devstral-small | 12/53 | 22.6% | 21 | 485 | 12.1 | 2453 | 38 | 0 | 57.5M / 0.21M |
| nemotron-super-fix | 15/53 | 28.3% | 26 | 830 | 14.1 | 1473 | 7 | 1 | 34.3M / 0.13M |

## S4 — Aider Polyglot (225 exercises)

opencode 1.18.35 in each task container, 4 trials in parallel. Silent stops = failed trials whose last turn ended after reasoning only (no tool call, no text): the model quit early instead of failing at the work.

| Model | Solved | Rate | Exceptions | Median agent s | Agent hours | Tool calls | Tool errors | Silent stops (failed) | Tokens in / out |
|---|---|---|---|---|---|---|---|---|---|
| qwen36 | 97/225 | 43.1% | 19 | 62 | 8.6 | 3078 | 53 | 1 | 49.1M / 0.63M |
| qwen36-fp8 | 108/225 | 48.0% | 15 | 111 | 14.3 | 2646 | 338 | 1 | 39.5M / 0.48M |
| coder-next | 101/225 | 44.9% | 21 | 32 | 4.8 | 1743 | 82 | 6 | 27.8M / 0.42M |
| coder-next-fp8 | 108/225 | 48.0% | 23 | 51 | 9.0 | 2710 | 342 | 5 | 59.4M / 0.60M |
| flash | 150/225 | 66.7% | 18 | 333 | 30.0 | 2586 | 195 | 0 | 41.3M / 0.55M |
| nemotron-super | 67/225 | 29.8% | 39 | 630 | 46.5 | 3398 | 99 | 29 | 53.1M / 2.01M |
| devstral-small | 51/225 | 22.7% | 78 | 573 | 52.6 | 16569 | 164 | 0 | 257.0M / 1.33M |
| nemotron-super-fix | 62/225 | 27.6% | 43 | 658 | 50.3 | 4202 | 371 | 6 | 69.1M / 0.73M |

Per language:

| Language | qwen36 | qwen36-fp8 | coder-next | coder-next-fp8 | flash | nemotron-super | devstral-small | nemotron-super-fix |
|---|---|---|---|---|---|---|---|---|
| cpp | 26.9% | 42.3% | 23.1% | 38.5% | 50.0% | 15.4% | 3.8% | 11.5% |
| go | 33.3% | 35.9% | 30.8% | 33.3% | 64.1% | 23.1% | 10.3% | 25.6% |
| java | 12.8% | 21.3% | 23.4% | 14.9% | 44.7% | 10.6% | 6.4% | 10.6% |
| javascript | 71.4% | 71.4% | 75.5% | 87.8% | 89.8% | 51.0% | 55.1% | 42.9% |
| python | 44.1% | 44.1% | 47.1% | 50.0% | 64.7% | 8.8% | 2.9% | 14.7% |
| rust | 70.0% | 76.7% | 63.3% | 60.0% | 83.3% | 70.0% | 50.0% | 60.0% |

## S5 — Custom technical tasks (12)

opencode 1.18.35 in each task container, 4 trials in parallel. Silent stops = failed trials whose last turn ended after reasoning only (no tool call, no text): the model quit early instead of failing at the work.

| Model | Solved | Rate | Exceptions | Median agent s | Agent hours | Tool calls | Tool errors | Silent stops (failed) | Tokens in / out |
|---|---|---|---|---|---|---|---|---|---|
| qwen36 | 9/12 | 75.0% | 3 | 219 | 0.8 | 369 | 1 | 0 | 7.6M / 0.05M |
| qwen36-fp8 | 7/12 | 58.3% | 3 | 374 | 1.7 | 429 | 4 | 0 | 8.5M / 0.06M |
| coder-next | 6/12 | 50.0% | 2 | 274 | 1.4 | 743 | 9 | 1 | 26.3M / 0.10M |
| coder-next-fp8 | 7/12 | 58.3% | 3 | 343 | 1.7 | 584 | 11 | 0 | 16.2M / 0.08M |
| flash | 12/12 | 100.0% | 2 | 414 | 1.6 | 191 | 1 | 0 | 3.3M / 0.04M |
| nemotron-super | 6/12 | 50.0% | 2 | 732 | 2.5 | 254 | 0 | 3 | 5.4M / 0.09M |
| devstral-small | 3/12 | 25.0% | 1 | 221 | 1.3 | 345 | 7 | 0 | 4.9M / 0.03M |
| flash-fp4l | 12/12 | 100.0% | 4 | 585 | 2.1 | 316 | 1 | 0 | 7.0M / 0.07M |
| nemotron-super-fix | 5/12 | 41.7% | 4 | 1010 | 3.4 | 390 | 3 | 0 | 8.0M / 0.04M |

## Live load (server-side, sampled every 15 s while each suite ran)

Busy = intervals with requests running. Per-stream tok/s = generated tokens ÷ request-seconds (running requests × time), i.e. the average speed each session saw, prefill included. The S3/S4/S5 rows are the real 4-agent case. KV = KV-cache usage (p95 / max); preemptions = requests evicted because KV memory ran out.

| Model | Suite | Busy h | Mean running | Time at 4 running | Time with queue | Gen tok/s total | Per-stream tok/s | TTFT s | Queue s | KV p95 / max | Preemptions | Prefix hit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen36 | s0 | 0.05 | 1.7 | 7.7% | 7.7% | 101.7 | 60.0 | 1.06 | 0.04 | 3.7% / 3.7% | 0 | 16.5% |
| qwen36 | s1 | 0.10 | 1.4 | 0.0% | 0.0% | 125.3 | 92.2 | 0.50 | 0.00 | 1.8% / 2.0% | 0 | 58.7% |
| qwen36 | s2 | 2.56 | 4.0 | 97.2% | 0.0% | 271.2 | 68.4 | 0.26 | 0.00 | 3.9% / 4.2% | 0 | 0.0% |
| qwen36 | s3 | 3.25 | 2.2 | 13.1% | 3.1% | 125.7 | 57.5 | 1.63 | 0.23 | 8.3% / 11.1% | 0 | 86.8% |
| qwen36 | s4 | 3.41 | 2.4 | 23.4% | 11.6% | 145.8 | 61.6 | 1.41 | 0.59 | 5.4% / 7.6% | 0 | 76.0% |
| qwen36 | s5 | 0.75 | 2.3 | 22.6% | 10.5% | 133.9 | 58.4 | 1.76 | 0.64 | 6.1% / 6.8% | 0 | 84.3% |
| qwen36 | s6 | 4.11 | 4.0 | 97.3% | 0.0% | 279.5 | 70.6 | 0.25 | 0.00 | 3.8% / 4.3% | 0 | 0.0% |
| qwen36 | s6nt | 1.27 | 3.9 | 92.4% | 0.0% | 263.1 | 67.4 | 0.27 | 0.00 | 3.8% / 4.1% | 0 | 0.0% |
| qwen36 | s7 | 0.17 | 1.0 | 0.0% | 0.0% | 112.7 | 112.7 | 0.63 | 0.00 | 1.5% / 1.6% | 0 | 0.0% |
| qwen36-fp8 | s0 | 0.09 | 2.0 | 31.8% | 4.5% | 62.5 | 31.3 | 1.32 | 0.11 | 4.9% / 5.7% | 0 | 16.5% |
| qwen36-fp8 | s1 | 0.19 | 1.5 | 0.0% | 0.0% | 71.9 | 47.9 | 0.67 | 0.00 | 2.2% / 2.3% | 0 | 56.9% |
| qwen36-fp8 | s2 | 5.44 | 4.0 | 98.2% | 0.0% | 128.8 | 32.5 | 0.47 | 0.00 | 4.4% / 4.8% | 0 | 0.0% |
| qwen36-fp8 | s3 | 3.35 | 2.5 | 16.2% | 5.4% | 79.2 | 31.4 | 2.08 | 0.72 | 7.7% / 9.3% | 0 | 81.0% |
| qwen36-fp8 | s4 | 5.37 | 2.7 | 31.5% | 13.1% | 84.5 | 31.6 | 2.10 | 1.10 | 5.9% / 7.5% | 0 | 73.6% |
| qwen36-fp8 | s5 | 0.61 | 2.7 | 36.7% | 15.6% | 81.3 | 29.8 | 2.87 | 1.52 | 6.1% / 7.9% | 0 | 81.8% |
| qwen36-fp8 | s6 | 8.93 | 4.0 | 97.8% | 0.0% | 133.5 | 33.7 | 0.46 | 0.00 | 4.5% / 5.2% | 0 | 0.0% |
| qwen36-fp8 | s6nt | 2.45 | 3.9 | 93.2% | 0.0% | 126.8 | 32.5 | 0.47 | 0.00 | 4.2% / 4.5% | 0 | 0.0% |
| qwen36-fp8 | s7 | 0.35 | 1.0 | 0.0% | 0.0% | 56.2 | 56.2 | 0.80 | 0.00 | 1.8% / 2.0% | 0 | 0.0% |
| coder-next | s0 | 0.10 | 2.0 | 32.0% | 4.0% | 54.8 | 27.9 | 1.75 | 0.24 | 3.9% / 4.1% | 0 | 28.5% |
| coder-next | s1 | 0.10 | 0.9 | 0.0% | 0.0% | 32.5 | 36.9 | 0.54 | 0.00 | 0.6% / 0.8% | 0 | 75.0% |
| coder-next | s2 | 2.08 | 3.8 | 91.8% | 0.0% | 106.7 | 28.0 | 0.23 | 0.00 | 1.8% / 2.3% | 0 | 67.0% |
| coder-next | s3 | 4.03 | 2.1 | 11.0% | 0.1% | 50.1 | 24.2 | 0.76 | 0.01 | 10.6% / 16.8% | 0 | 96.1% |
| coder-next | s4 | 2.53 | 1.6 | 2.8% | 0.0% | 48.2 | 29.2 | 0.45 | 0.00 | 5.4% / 7.9% | 0 | 93.2% |
| coder-next | s5 | 0.50 | 2.4 | 18.2% | 0.0% | 55.6 | 23.1 | 0.63 | 0.01 | 8.2% / 9.2% | 0 | 97.2% |
| coder-next | s6 | 4.22 | 3.9 | 96.4% | 0.1% | 111.2 | 28.2 | 0.27 | 0.01 | 2.3% / 3.1% | 0 | 16.3% |
| coder-next | s7 | 0.21 | 1.0 | 0.0% | 0.0% | 37.0 | 37.0 | 0.87 | 0.00 | 1.4% / 1.6% | 0 | 0.0% |
| coder-next-fp8 | s0 | 0.12 | 2.1 | 37.9% | 0.0% | 46.6 | 21.8 | 2.32 | 0.28 | 6.4% / 6.7% | 0 | 28.5% |
| coder-next-fp8 | s1 | 0.11 | 0.8 | 0.0% | 0.0% | 31.6 | 38.8 | 0.89 | 0.00 | 1.0% / 1.0% | 0 | 77.1% |
| coder-next-fp8 | s2 | 3.23 | 3.9 | 94.6% | 0.0% | 73.4 | 18.8 | 0.43 | 0.00 | 3.0% / 4.0% | 0 | 67.0% |
| coder-next-fp8 | s3 | 3.74 | 2.1 | 6.4% | 0.4% | 42.7 | 20.6 | 0.99 | 0.01 | 12.8% / 17.2% | 0 | 95.1% |
| coder-next-fp8 | s4 | 3.82 | 2.1 | 8.3% | 0.3% | 43.8 | 21.3 | 0.80 | 0.01 | 7.8% / 12.2% | 0 | 95.3% |
| coder-next-fp8 | s5 | 0.54 | 2.9 | 38.0% | 0.8% | 44.3 | 15.4 | 1.06 | 0.01 | 13.5% / 14.8% | 0 | 96.2% |
| coder-next-fp8 | s6 | 6.26 | 4.0 | 97.9% | 0.0% | 76.1 | 19.1 | 0.50 | 0.00 | 3.6% / 4.9% | 0 | 16.3% |
| coder-next-fp8 | s7 | 0.22 | 1.0 | 0.0% | 0.0% | 39.8 | 41.4 | 1.65 | 0.00 | 2.4% / 2.4% | 0 | 0.0% |
| flash | s0 | 0.16 | 2.0 | 30.8% | 0.0% | 34.6 | 17.1 | 2.86 | 0.19 | 27.8% / 29.4% | 0 | 18.4% |
| flash | s1 | 0.30 | 1.1 | 0.0% | 0.0% | 40.7 | 37.0 | 1.23 | 0.00 | 9.8% / 11.0% | 0 | 67.3% |
| flash | s2 | 7.07 | 3.9 | 95.8% | 0.0% | 87.4 | 22.3 | 0.69 | 0.00 | 25.4% / 28.7% | 0 | 0.0% |
| flash | s3 | 4.64 | 2.4 | 19.6% | 1.8% | 54.2 | 22.2 | 2.08 | 0.32 | 46.5% / 58.1% | 0 | 85.3% |
| flash | s4 | 16.61 | 3.0 | 37.0% | 5.9% | 65.4 | 21.6 | 2.36 | 0.66 | 31.6% / 39.4% | 0 | 77.8% |
| flash | s5 | 0.59 | 2.6 | 35.9% | 7.0% | 59.3 | 22.6 | 3.16 | 1.31 | 30.0% / 31.8% | 0 | 82.3% |
| flash | s6 | 9.38 | 4.0 | 96.8% | 2.4% | 89.3 | 22.5 | 1.29 | 0.60 | 24.5% / 26.9% | 0 | 1.6% |
| flash | s6nt | 3.06 | 3.9 | 92.0% | 0.0% | 96.5 | 24.7 | 0.68 | 0.00 | 20.5% / 22.0% | 0 | 0.0% |
| flash | s7 | 1.18 | 1.0 | 0.0% | 0.0% | 36.9 | 37.0 | 1.83 | 0.00 | 10.4% / 11.9% | 0 | 0.0% |
| nemotron-super | s0 | 0.18 | 2.2 | 34.9% | 0.0% | 31.9 | 14.3 | 3.78 | 0.27 | 15.0% / 16.5% | 0 | 11.1% |
| nemotron-super | s1 | 0.77 | 1.1 | 0.0% | 0.0% | 25.7 | 23.9 | 3.31 | 0.00 | 6.7% / 7.6% | 0 | 38.5% |
| nemotron-super | s2 | 6.92 | 3.8 | 92.2% | 0.0% | 65.1 | 17.0 | 1.03 | 0.00 | 14.0% / 14.8% | 0 | 0.0% |
| nemotron-super | s3 | 3.96 | 2.8 | 34.5% | 6.7% | 36.7 | 13.1 | 5.08 | 1.01 | 16.5% / 18.4% | 0 | 59.7% |
| nemotron-super | s4 | 13.44 | 3.4 | 52.6% | 12.1% | 45.1 | 13.4 | 5.74 | 1.78 | 18.0% / 20.8% | 0 | 51.2% |
| nemotron-super | s5 | 0.80 | 2.8 | 34.2% | 14.5% | 34.9 | 12.2 | 6.58 | 2.40 | 15.9% / 16.8% | 0 | 63.5% |
| nemotron-super | s6 | 9.14 | 3.9 | 96.2% | 0.0% | 69.2 | 17.6 | 1.02 | 0.00 | 16.5% / 17.2% | 0 | 0.0% |
| nemotron-super | s6nt | 6.81 | 3.9 | 94.4% | 0.0% | 69.1 | 17.7 | 1.06 | 0.00 | 16.5% / 17.8% | 0 | 0.0% |
| nemotron-super | s7 | 0.54 | 1.0 | 0.0% | 0.0% | 26.9 | 26.9 | 2.23 | 0.00 | 4.2% / 5.5% | 0 | 0.0% |
| devstral-small | s0 | 0.34 | 2.0 | 29.6% | 1.2% | 17.1 | 8.4 | 2.26 | 0.19 | 12.9% / 13.1% | 0 | 42.2% |
| devstral-small | s1 | 1.99 | 1.0 | 0.0% | 0.0% | 8.7 | 8.9 | 0.31 | 0.00 | 3.8% / 4.4% | 0 | 97.0% |
| devstral-small | s2 | 3.18 | 3.5 | 82.2% | 0.0% | 30.8 | 8.8 | 0.32 | 0.00 | 6.7% / 8.7% | 0 | 82.1% |
| devstral-small | s3 | 4.04 | 2.2 | 10.7% | 0.3% | 16.3 | 7.3 | 0.67 | 0.03 | 33.0% / 38.0% | 0 | 98.7% |
| devstral-small | s4 | 15.39 | 3.2 | 43.6% | 0.1% | 26.5 | 8.2 | 0.48 | 0.00 | 12.4% / 19.1% | 0 | 99.5% |
| devstral-small | s5 | 0.45 | 2.7 | 19.4% | 0.0% | 21.5 | 8.0 | 0.60 | 0.02 | 10.8% / 11.5% | 0 | 97.7% |
| devstral-small | s6 | 6.11 | 4.0 | 97.8% | 0.0% | 35.5 | 9.0 | 0.30 | 0.00 | 7.3% / 10.7% | 0 | 86.8% |
| devstral-small | s7 | 0.61 | 1.0 | 0.0% | 0.0% | 8.9 | 8.9 | 1.51 | 0.00 | 5.3% / 5.5% | 0 | 8.7% |
| flash-fp4l | s0 | 0.15 | 2.3 | 40.0% | 0.0% | 38.9 | 17.2 | 2.24 | 0.11 | 17.3% / 17.6% | 0 | 30.4% |
| flash-fp4l | s1 | 0.46 | 1.1 | 0.0% | 0.0% | 47.2 | 43.3 | 0.78 | 0.00 | 7.9% / 8.2% | 0 | 91.1% |
| flash-fp4l | s5 | 0.75 | 2.8 | 20.0% | 0.0% | 69.0 | 24.8 | 0.92 | 0.00 | 21.5% / 24.1% | 0 | 97.1% |
| nemotron-super-fix | s3 | 4.29 | 2.9 | 33.9% | 10.8% | 34.3 | 12.0 | 5.25 | 1.16 | 17.9% / 20.6% | 0 | 69.4% |
| nemotron-super-fix | s4 | 14.35 | 3.4 | 52.8% | 12.7% | 43.3 | 12.9 | 5.58 | 1.62 | 16.6% / 18.5% | 0 | 54.5% |
| nemotron-super-fix | s5 | 1.01 | 3.0 | 47.3% | 12.8% | 35.8 | 11.8 | 5.63 | 1.61 | 17.2% / 17.9% | 0 | 64.9% |

## S7 — Long-form technical writing (blind pairwise, Claude judge)

Win rate = (W + ½T) / comparisons; a comparison is judged in both orders, and disagreement counts as a tie. 240/240 pair files judged; order agreement 88.3%; raw verdicts A/B/tie 119/119/2. Rubric 1–10. Details: results/writing/scores.json.

| Model | Win rate | W-T-L | Correctness | Completeness | Actionability | Structure | Concision | Rubric mean |
|---|---|---|---|---|---|---|---|---|
| flash | 81.2% | 30-5-5 | 5.80 | 7.12 | 6.89 | 7.84 | 7.03 | 6.93 |
| nemotron-super | 62.5% | 23-4-13 | 5.06 | 7.55 | 6.53 | 7.29 | 5.65 | 6.42 |
| qwen36 | 60.0% | 22-4-14 | 4.90 | 6.35 | 5.89 | 6.92 | 6.21 | 6.05 |
| coder-next | 23.8% | 7-5-28 | 3.94 | 6.49 | 5.50 | 7.00 | 6.12 | 5.81 |
| devstral-small | 22.5% | 6-6-28 | 4.14 | 5.44 | 5.09 | 6.33 | 6.36 | 5.47 |

Extra head-to-heads (not in the ranking or the rubric means above):

| Pair | W-T-L (first) | Win rate (first) | Rubric mean (first / second) |
|---|---|---|---|
| qwen36 vs qwen36-fp8 | 1-2-7 | 20% | 5.89 / 6.25 |
| coder-next vs coder-next-fp8 | 4-1-5 | 45% | 5.94 / 5.98 |

## Run log (suite exit codes and wall time)

| Model | Suite | rc | Hours | Finished |
|---|---|---|---|---|
| qwen36 | smoke | 0 | 0.00 | 2026-10-10T19:48 |
| qwen36 | s0 | 0 | 0.06 | 2026-10-10T19:52 |
| qwen36 | s1 | 0 | 0.11 | 2026-10-10T19:58 |
| qwen36 | s2 | 0 | 2.57 | 2026-10-10T22:33 |
| qwen36 | s7 | 0 | 0.17 | 2026-10-10T22:43 |
| qwen36 | smoke | 0 | 0.00 | 2026-10-10T23:28 |
| qwen36 | s5 | 0 | 0.35 | 2026-10-10T23:49 |
| qwen36 | s3 | 0 | 3.51 | 2026-10-11T03:19 |
| qwen36 | s4 | 0 | 3.62 | 2026-10-11T06:56 |
| qwen36 | s6 | 0 | 4.13 | 2026-10-11T11:04 |
| qwen36 | s6nt | 0 | 1.28 | 2026-10-11T12:21 |
| coder-next | smoke | 0 | 0.00 | 2026-10-11T12:28 |
| coder-next | s0 | 0 | 0.11 | 2026-10-11T12:35 |
| coder-next | s1 | 0 | 0.11 | 2026-10-11T12:42 |
| coder-next | s2 | 0 | 2.09 | 2026-10-11T14:47 |
| coder-next | s7 | 0 | 0.21 | 2026-10-11T15:00 |
| coder-next | s5 | 0 | 0.54 | 2026-10-11T15:32 |
| coder-next | s3 | 0 | 4.24 | 2026-10-11T19:46 |
| coder-next | s4 | 0 | 2.94 | 2026-10-11T22:43 |
| coder-next | s6 | 0 | 4.23 | 2026-10-12T02:57 |
| coder-next | s6nt | 0 | 0.00 | 2026-10-12T02:57 |
| devstral-small | smoke | 0 | 0.00 | 2026-10-12T03:03 |
| devstral-small | s0 | 0 | 0.34 | 2026-10-12T03:23 |
| devstral-small | s1 | 0 | 2.00 | 2026-10-12T05:23 |
| devstral-small | s2 | 0 | 3.18 | 2026-10-12T08:34 |
| devstral-small | s7 | 0 | 0.61 | 2026-10-12T09:11 |
| devstral-small | s5 | 0 | 0.49 | 2026-10-12T09:40 |
| devstral-small | s3 | 0 | 4.30 | 2026-10-12T13:58 |
| nemotron-super | smoke | 0 | 0.00 | 2026-10-12T17:13 |
| nemotron-super | s0 | 0 | 0.19 | 2026-10-12T17:25 |
| nemotron-super | s1 | 0 | 0.78 | 2026-10-12T18:11 |
| nemotron-super | s2 | 0 | 6.92 | 2026-10-13T01:07 |
| nemotron-super | s7 | 0 | 0.54 | 2026-10-13T01:39 |
| nemotron-super | s5 | 0 | 0.85 | 2026-10-13T02:30 |
| nemotron-super | s3 | 0 | 4.03 | 2026-10-13T06:32 |
| nemotron-super | smoke | 0 | 0.00 | 2026-10-13T17:53 |
| nemotron-super | s4 | 0 | 8.62 | 2026-10-14T02:31 |
| nemotron-super | s6 | 0 | 9.15 | 2026-10-14T11:40 |
| nemotron-super | s6nt | 0 | 6.82 | 2026-10-14T18:29 |
| flash | smoke | 0 | 0.00 | 2026-10-14T18:33 |
| flash | s0 | 0 | 0.17 | 2026-10-14T18:43 |
| flash | s1 | 0 | 0.30 | 2026-10-14T19:01 |
| flash | s2 | 0 | 7.07 | 2026-10-15T02:05 |
| flash | s7 | 0 | 1.19 | 2026-10-15T03:17 |
| flash | s5 | 0 | 0.63 | 2026-10-15T03:54 |
| flash | s3 | 0 | 4.76 | 2026-10-15T08:40 |
| flash | s4-web | 0 | 7.45 | 2026-10-15T16:07 |
| flash | s6 | 0 | 9.39 | 2026-10-16T01:31 |
| flash | s6nt | 0 | 3.07 | 2026-10-16T04:35 |
| qwen36-fp8 | smoke | 0 | 0.00 | 2026-10-16T04:43 |
| qwen36-fp8 | s0 | 0 | 0.10 | 2026-10-16T04:49 |
| qwen36-fp8 | s1 | 0 | 0.20 | 2026-10-16T05:01 |
| qwen36-fp8 | s2 | 0 | 5.44 | 2026-10-16T10:27 |
| qwen36-fp8 | s7 | 0 | 0.35 | 2026-10-16T10:48 |
| qwen36-fp8 | s5 | 0 | 0.65 | 2026-10-16T11:27 |
| qwen36-fp8 | s3 | 0 | 3.44 | 2026-10-16T14:54 |
| qwen36-fp8 | s4 | 0 | 5.46 | 2026-10-16T20:22 |
| qwen36-fp8 | s6 | 0 | 8.94 | 2026-10-17T05:18 |
| qwen36-fp8 | s6nt | 0 | 2.46 | 2026-10-17T07:45 |
| coder-next-fp8 | smoke | 0 | 0.00 | 2026-10-17T07:54 |
| coder-next-fp8 | s0 | 0 | 0.13 | 2026-10-17T08:02 |
| coder-next-fp8 | s1 | 0 | 0.12 | 2026-10-17T08:09 |
| coder-next-fp8 | s2 | 0 | 3.24 | 2026-10-17T11:23 |
| coder-next-fp8 | s7 | 0 | 0.22 | 2026-10-17T11:36 |
| coder-next-fp8 | s5 | 0 | 0.60 | 2026-10-17T12:13 |
| coder-next-fp8 | s3 | 0 | 3.91 | 2026-10-17T16:07 |
| coder-next-fp8 | s4 | 0 | 3.95 | 2026-10-17T20:04 |
| coder-next-fp8 | s6 | 0 | 6.27 | 2026-10-18T02:20 |
| coder-next-fp8 | s6nt | 0 | 0.00 | 2026-10-18T02:20 |
| devstral-small | smoke | 0 | 0.00 | 2026-10-18T02:25 |
| devstral-small | s4 | 0 | 12.40 | 2026-10-18T14:49 |
| devstral-small | s6 | 0 | 6.12 | 2026-10-18T20:57 |
| devstral-small | s6nt | 0 | 0.00 | 2026-10-18T20:57 |
| nemotron-super-fix | smoke | 0 | 0.00 | 2026-10-18T21:10 |
| nemotron-super-fix | s5 | 0 | 1.11 | 2026-10-18T22:16 |
| nemotron-super-fix | s3 | 0 | 4.33 | 2026-10-19T02:36 |
| nemotron-super-fix | s4 | 0 | 14.38 | 2026-10-19T16:59 |
| flash | smoke | 0 | 0.00 | 2026-10-19T17:03 |
| flash | s4 | 0 | 9.26 | 2026-10-20T02:19 |
| flash-fp4l | smoke | 0 | 0.00 | 2026-10-20T12:28 |
| flash-fp4l | s0 | 0 | 0.15 | 2026-10-20T12:37 |
| flash-fp4l | s1 | 0 | 0.46 | 2026-10-20T13:05 |
| flash-fp4l | s5 | 0 | 0.80 | 2026-10-20T13:52 |

<!-- END AUTO -->

## Analysis and recommendation

_Final analysis, 2026-10-20, after all 7 runs and S7 judging. The per-model notes further down have the detail. The Design A/B decision is the owner's; this is the recommendation._

### Recommendation in one paragraph

**Design A with two switchable modes: qwen36 as Daily (fast, always on) and Flash as Heavy (quality, on demand).** This matches the goal of having the dashboard switch between "fast/interactive" and "hand it off/quality" work. Flash is clearly the most capable model on every agentic and writing suite; qwen36 is clearly the fastest and second on most quality measures. No other candidate earns a place. **Thinking off by default in both modes**, with thinking on demand for qwen36 only. The one thing to know: the two models don't fit in memory together, so a switch is a model swap of about 4–5 minutes, during which nothing is served.

### Results at a glance (5 base models)

| | qwen36 | flash | nemotron-super ¹ | coder-next | devstral-small |
|---|---|---|---|---|---|
| **S3 Terminal-Bench (53)** | 37.7 % | **58.5 %** | 20.8 % (28.3 % fixed parser) | 30.2 % | 22.6 % |
| **S4 Polyglot (225)** | 43.1 % | **66.2 %** ² | 29.8 % (27.6 %) | 44.9 % | 22.7 % |
| **S5 custom tasks (12)** | 9 | **12** | 6 (5) | 6 | 3 |
| S1 mini agent (8) | 7 | 8 | 8 | 6 | 7 |
| **S7 writing win rate** | 60 % | **81 %** ³ | 62.5 % | 24 % | 22.5 % |
| S2 MMLU-Pro CS / eng | **89 / 80** | 87.5 / 75.5 | 88 / 73 | 85.5 / 74.5 | 73 / 44 |
| S2 IFEval strict | 90.5 % | **91.5 %** | 90 % | 78 % | 65.5 % |
| S6 GPQA, thinking on / off | **80.8** / 78.3 | 65.7 / **83.3** | 64.1 / 63.1 | 75.8 / – | 52.5 / – |
| S6 BBH, thinking on | 94.2 % | 94.4 % | 92.4 % | 91.9 % | 85.6 % |
| **Speed, 1 stream (real text)** | **105 tok/s** | 43 | 27 | 38 | 9 |
| **Speed per agent, 4 agents (live)** | **~60 tok/s** | ~22 | ~13 | ~23–29 | ~9 |
| Median S3 / S5 / S4 task time | **509 / 219 / 62 s** | 754 / 414 / 333 s | 826 / 732 / 630 s | 389 / 274 / 32 s | 485 / 221 / 573 s |
| Memory, load time | 73 GiB, 280 s | 102 GiB, 210 s | 99 GiB, 686 s | 78 GiB, 381 s | 71 GiB, 275 s |

¹ "As shipped" (NVIDIA's reasoning parser); fixed-parser rerun in brackets. ² Web blocked, 1 leaked pass removed (raw 66.7 %). ³ Written with thinking off (with thinking on it ran out of the 16k budget).

### Why these two, and why not the others

- **Flash is the quality model by a wide margin**, and the margin is on the work that matters for this Spark (agentic coding, sysadmin and databases): S3 +21 points, Polyglot +23, S5 12/12, writing 81 % with wins against every model.
  - Its knowledge scores (S2) are no higher than qwen36's. The gap is in doing the work: fewest tool errors (2 in 1,049 S3 calls), no silent stops, and good instruction following.
- **Flash's cost is speed**, but less than raw tok/s suggests.
  - Raw: ~40 % of qwen36 single-stream, ~35 % per agent with 4 agents.
  - Per task it uses fewer tool calls and reads less (S3: 26M input tokens vs 58M), so on the hard suites it takes **1.5× (S3) and 1.9× (S5)** as long as qwen36. Only on short Polyglot exercises is it ~5× (333 s vs 62 s).
- **qwen36 is the fast model and the second-best all-rounder:** the best S2 knowledge, second on S3/S4/S5, tied second on writing, and GPQA 80.8 %. It is 2–3× faster than any other candidate under agent load.
- **nemotron-super: no.** It is ~4–8× slower than qwen36 and behind it on every agentic suite. Fixing the parser didn't change that (S3 15 vs 11, Polyglot 62 vs 67). Its strengths (security knowledge, writing 62.5 %) don't come close to Flash's.
- **coder-next: no.** Slower than qwen36 and lower on almost everything; its Polyglot edge over qwen36 (+1.8 points) is noise. Writing 24 %.
- **devstral-small: no.** Last or near-last everywhere, and the slowest (9 tok/s, FP8 dense).
- **The fp8 variants: no.** They show no consistent quality gain over NVFP4 (better on Polyglot and writing, worse on S3 and S5; all within noise for these sample sizes) at half the speed and more memory. NVFP4 is the right format on this hardware.

### Against the criteria for Design B (Big = Flash, Fast = qwen36)

Design B means Flash as the only model, always on.

1. **Knowledge (Big ≥ 5 points over Fast on S2 MMLU-Pro CS + engineering): not met.** Flash 81.5 vs qwen36 84.5. Flash's engineering score is held down by 39 answers that ran out of thinking budget, but even so S2 doesn't separate them. This criterion assumed the bigger model would win on knowledge; here the difference is in agentic work instead.
2. **Agent quality (Big ≥ 10 points on S3): met.** +20.8 points.
3. **4-agent usability (median S3 task time ≤ 2× Fast, no OOM or preemptions): met.** 1.48×, 0 preemptions, KV usage peaked at 58 %. Stalls over 60 s weren't measured directly; the mean queue wait in S3 was 0.3 s.
4. **Headroom for the router: no longer applies.** The router was dropped from the design on 2026-10-20 (see below). Flash takes 102 GiB of ~121; headroom for an always-on helper or embeddings model is measured when one is added.

So by these criteria, Design B isn't proven. That fits the goal of switchable modes: **Design A, run as two modes.**

### How the two modes would work

| Mode | Model | Use it for | Settings |
|---|---|---|---|
| **Fast** (Daily, default) | qwen36 | Interactive work: you watch and steer each step; quick questions; short edits | Thinking off by default (S6: BBH equal, GPQA −2.5 points, ~3× faster); thinking on demand |
| **Quality** (Heavy, on demand) | Flash | Hand-off work: start a task and come back later; hard multi-step jobs; documents | Thinking off (GPQA 83 % off vs 66 % on with a 16k budget; on is slow and often doesn't finish) |

- **Switching cost:** stop one model and load the other. Flash loads in ~210 s, qwen36 in ~280 s, plus a few seconds for memory to return.
  - **Running sessions are cut off** by a switch, so the dashboard should show what's running and warn before swapping.
  - Known issue: Flash's first boot can fail right after another large model stops; wait for memory to return and retry once (in serve.sh notes).
- **They can't run side by side:** 73 + 102 GiB > 121 GiB. qwen36's 73 GiB is mostly KV cache, so a smaller qwen36 alongside a leaner Flash might fit, but that is unmeasured, and it would cut Flash's KV room for agents.
- **No router (decided 2026-10-20):** the two models can't be loaded together, so per-request routing would force 4–5 min swaps that cut off running sessions. The mode is chosen per task with a manual switch on the dashboard, and `model: auto` points at whichever model is loaded. Revisit only if an always-on helper or embeddings model is added.

### Open items before deploying

1. ~~Router headroom~~: dropped along with the router (2026-10-20).
2. **Flash serving recipe (tested 2026-10-20, `flash-fp4l`):** the r/DGX_Spark recipe gives ~2.9× the KV room (1.23M tokens vs ~430k) and ~10 % more speed, with the same quality (S1 8/8, S5 12/12). But agent tasks took ~40 % longer (S5 median 585 s vs 414; ~65 % more tool calls), and it uses 11 GiB more memory by default. Not a drop-in win. Find out why it takes more steps (chat template, tool parser, sampling) before switching. Details in notes.md.
3. **S7 human check:** the owner blind-reviews pair files 0014, 0017, 0126, 0219.
4. **For any future benchmark run:** drop `proxy.golang.org` / `sum.golang.org` from the Polyglot allowlist, and set a random `ORACLE_SECRET` (or remove `.oracle/` from agent images). Flash probes for ways around the sandbox; keep that in mind if it ever runs agents with real network access.

### Interim notes per model (written as each model finished)

### S7 — Long-form technical writing (blind pairwise judging, 2026-10-20)

All pairs of the 5 base models: 10 prompts × 2 orders = 200 judgements, plus fp8-vs-base head-to-heads (40). Judged by 16 blind Claude subagents against `writing/JUDGE.md`; each saw only the pair files, and the two orders of one comparison always went to different judges. A comparison counts as a win only when both orders agree; disagreement counts as a tie. Data: `results/writing/scores.json`, `judgements.jsonl`.

| Model | Win rate | W-T-L | Correctness | Completeness | Actionability | Structure | Concision | Rubric mean |
|---|---|---|---|---|---|---|---|---|
| flash ¹ | **81.2 %** | 30-5-5 | 5.80 | 7.12 | 6.89 | 7.84 | 7.03 | 6.93 |
| nemotron-super | 62.5 % | 23-4-13 | 5.06 | 7.55 | 6.53 | 7.29 | 5.65 | 6.42 |
| qwen36 ² | 60.0 % | 22-4-14 | 4.90 | 6.35 | 5.89 | 6.92 | 6.21 | 6.05 |
| coder-next | 23.8 % | 7-5-28 | 3.94 | 6.49 | 5.50 | 7.00 | 6.12 | 5.81 |
| devstral-small | 22.5 % | 6-6-28 | 4.14 | 5.44 | 5.09 | 6.33 | 6.36 | 5.47 |

¹ **Flash wrote with thinking off** (decided 2026-10-20). With thinking on it used up the 16k budget on 9/10 prompts and left 7 answers empty (notes.md). All other models wrote with thinking on, or have no thinking mode.
² qwen36's prompt 04 answer is empty (a reasoning repetition loop), so it loses all 4 comparisons on that prompt. On the other 9 prompts it is 22-4-10.

- **Head to head:** Flash beats every model (70–95 %). nemotron-super and qwen36 are close (45/55 between them). coder-next and devstral-small are clearly last, and about equal to each other.
- **The judges are strict on correctness** (means 3.9–5.8 out of 10). Most answers contain at least one wrong command or step, and that is what separates the models. nemotron-super scores high on completeness but low on concision: it writes the longest answers (22.5k words over 10 prompts, vs ~11k for qwen36).
- **Quality checks:** no position bias (raw verdicts A 119 / B 119 / tie 2); the two orders agreed in 106/120 comparisons (88 %). The rationales cite concrete errors, e.g. `set -e` vs an ERR trap, a PG17 standby streaming from a PG13 primary, a null `$script:DryRun` in a remote scriptblock.
- **fp8 vs NVFP4:** qwen36-fp8 beat qwen36 7-2-1 (rubric 6.25 vs 5.89). coder-next-fp8 vs coder-next 5-1-4, a wash. There is one generation per prompt at temperature 0.6, so 10 comparisons can't separate precision from sampling luck. qwen36-fp8's prompt 04 is empty too (same failure), so that comparison is a tie.
- **Screening guide (win rate < 25 % = clearly the worst writer):** coder-next and devstral-small are both below it.
- **Human check still to do:** the owner to blind-review 3–4 pair files.

### qwen36 (Qwen3.6-35B-A3B NVFP4): done 2026-10-11, 15.8 h

- **Speed:** 109 tok/s single stream, 55 tok/s per stream at 4 concurrent (S0). Under real 4-agent load (S3–S5) each session saw ~58–62 tok/s, TTFT ~1.5 s. KV cache never above 11 %, no preemptions, prefix-cache hits 76–87 % in agent work. **Capacity is not a constraint for this model.**
- **Agentic work (the workload that matters):**
  - S5 custom sysadmin/DB/security: 9/12.
  - S3 Terminal-Bench: 20/53 (37.7 %).
  - S4 Polyglot: 97/225 (43.1 %), with JS 71 % and Rust 70 % but Java 13 % and C++ 27 % (tests hidden from the agent; spec details missed).
  - S1: 14/16.
  - Tool calling clean: 11 tool errors in 1,754 S3 calls, no malformed calls, 1 silent stop in 225 Polyglot trials.
- **Knowledge / reasoning:** MMLU-Pro CS 89 %, engineering 80 %, MMLU security 87 %, college CS 96 %, IFEval 90.5 % strict, BBH 94.2 %, GPQA diamond 80.8 %. These are high, so the agentic suites will separate the models better than these.
- **Thinking off costs almost nothing:** BBH 94.1 % vs 94.2 %, GPQA 78.3 % vs 80.8 %, at ~⅓ of the time. That argues for thinking-off as the Daily default, with thinking on demand.
- **Failure modes seen:**
  - A reasoning repetition loop (S7 prompt 04: same line 224×, no answer).
  - Reading several large files in one step and overflowing the context (S5 log-forensics); the agent then recovered and answered wrong.

### coder-next (Qwen3-Coder-Next 80B-A3B NVFP4, non-thinking): done 2026-10-12, 14.6 h

- **Speed is its weak point:** 37 tok/s single stream, 27 tok/s per stream at 4 (S0), and ~23–29 tok/s per session under real agent load. That's ⅓–½ of qwen36. It has no MTP draft head, so there's no speculative decoding; qwen36 gets ~3 tokens per step from MTP. Load 381 s, 77.5 GiB.
- **Agentic work:** behind qwen36 on the technical suites.
  - S5 6/12 (qwen36 9).
  - S3 16/53 = 30 % (qwen36 38 %), using 2× the input tokens and 50 tool errors (qwen36 11).
  - S4 101/225 = 44.9 % (qwen36 43.1 %): the only suite it edges out, mostly Java (23 % vs 13 %) and JS/Python; it's worse on Rust.
  - S1 14/16 (equal).
  - 6 silent stops in Polyglot, 1 in S5.
- **Knowledge / reasoning:** MMLU-Pro CS 85.5 %, engineering 74.5 %, MMLU security 76 % (9 prose answers with no option letter), college CS 96 %, IFEval 78 % strict, BBH 91.9 % (91.6 % before the 2026-10-14 rescorer fix), GPQA 75.8 %. All below qwen36. Thinking-off was skipped (no thinking mode).
- **Format compliance is weaker:** it answers BBH with "Final answer: **No**, Elanor does not…" or "Plausible" instead of the requested label, so stock lm-eval scored its BBH 0.1 %. The rescorer was extended (see notes.md).
- **Interim read:** no case for coder-next as Daily over qwen36. It's slower and lower on almost every measure; its Polyglot edge is within noise.

### nemotron-super (Nemotron 3 Super 120B-A12B NVFP4, "as shipped"): done 2026-10-14, ~38 h running

"As shipped" means NVIDIA's recommended serve recipe, including their `super_v3` reasoning-parser plugin. That parser has a bug that hides some tool calls (see notes.md, 2026-10-13). A fixed-parser rerun of the agent suites (**nemotron-super-fix**: S5/S3/S4) is queued at the end of the chain; agent numbers below will be reported both ways.

- **Speed is the main problem:** 23 tok/s single stream, 13 tok/s per stream at 4 (S0), ~13 tok/s per session under agent load. Load 686 s, 99 GiB. The whole model took ~38 h of running time (qwen36: 15.8 h), and Polyglot alone used 46.5 agent-hours against qwen36's 8.6.
- **Agentic work (as shipped):** clearly behind qwen36.
  - S5 6/12 (qwen36 9); S3 11/53 = 21 % (qwen36 38 %); S4 67/225 = 29.8 % (qwen36 43.1 %); S1 16/16 (the best so far).
  - Polyglot by language: Rust 21/30, JS 25/49, Go 9/39, Java 5/47, C++ 4/26, **Python 3/34** (qwen36 15/34).
  - **Python is a real result, not an artefact.** 30 of the 31 failures ran tests that failed on the interface: e.g. `recite()` returned one string per verse instead of one per line, and `grep()` returned a list instead of a string. With the tests hidden, nemotron guesses the API wrong more often. Only 1 Python trial was hit by the parser bug.
  - Parser-bug impact: hidden tool calls in 18/18 S3 silent stops and 5/5 S5 ones, but only 19/225 Polyglot trials (16 of them failed). **Even if the fix recovered every one, Polyglot would reach ~37 %, still below qwen36.** S3/S5 have more room: the fixed run decides those.
- **Knowledge / reasoning:**
  - MMLU-Pro CS 88 %, engineering 73 %, MMLU security 92 % (best so far), college CS 95 %, IFEval 90 % strict: on par with qwen36.
  - BBH 92.4 % thinking on / 91.5 % off (qwen36 94.2 / 94.1).
  - **GPQA diamond 64.1 % on / 63.1 % off** (qwen36 80.8 / 78.3). This is mostly a token budget effect: 58 (on) and 49 (off) of 198 questions have no answer because the model was still deliberating at the 16k-token limit (the same limit as every model; no repetition loops). With thinking "off" it simply reasons in the visible answer instead, up to 67k characters. On the 140 questions it did answer (thinking on), it got ~91 % right, but those are presumably the easier ones.
  - The same budget effect causes the 14 empty MMLU-Pro engineering answers and 13 BBH no-answers.
- **Thinking off doesn't save time for this model:** S6nt took 6.8 h vs S6 9.1 h, because it keeps reasoning in the answer.
- **Interim read:** as shipped, no case for nemotron-super as Daily over qwen36. It is ~4–8× slower, and it's behind on every agentic suite. Its strengths (security knowledge, S1, IFEval) don't outweigh that. Wait for nemotron-super-fix before closing it out, but the fix can't change speed.

### flash (Qwen3.8-Flash-Next NVFP4): done 2026-10-16; clean Polyglot (web blocked) done 2026-10-20

- **Polyglot, web blocked: 149/225 = 66.2 % (adjusted); raw 150/225 = 66.7 %.** That's the best of every model by ~18 points (qwen36 97, qwen36-fp8 / coder-next-fp8 108).
  - By language (raw): JS 44/49, Rust 25/30, Go 25/39, Python 22/34, Java 21/47, C++ 13/26. qwen36: 35 / 21 / 13 / 15 / 6 / 7. Flash gains most where the tests are hidden and the spec details matter (Java, Go, C++).
  - 18 exceptions (others 15–23), 0 silent stops, 195 tool errors in 2,586 calls. Slow: 30 agent-hours (median 333 s per trial; qwen36 62 s), 9.3 h wall.
  - **Adjustment:** Flash kept trying to reach Exercism content through the allowlist (87/225 trials tried). It succeeded in 1: go/scale-generator got the canonical test cases through the Go module proxy, which serves any GitHub repo. That pass is scored as a failure. Two other audit hits were checked by hand and are not leaks: java/hangman's zip download came back empty, and javascript/variable-length-quantity's hit was an npm search result with no test content.
  - It also tried to decrypt the task's encrypted reference solution (`/app/.oracle/solution.enc`) with guessed passwords in 7 trials (2 more in the with-web run). It never succeeded, and it never tried the actual default password. No adjustment for this.
  - The first run, with open internet, is archived in results-aborted/. Flash fetched hidden tests or reference solutions from GitHub in 80/225 trials there (78 passed). Not used.
- **Agentic work (clean): the strongest model so far by a wide margin.**
  - S5 **12/12** (qwen36 9); S3 Terminal-Bench **31/53 = 58.5 %** (qwen36 38 %); S1 16/16.
  - Only 2 tool errors in 1,049 S3 calls; 1 silent stop. Its S3/S5 web use was legitimate (tools and sources the tasks need), checked per trial.
- **Speed is its cost:** 28 tok/s single stream on random prompts, 43 on real text (MTP helps more on real text), 15 tok/s per stream at 4. Under agent load ~22 tok/s per session. Load 206 s, 102 GiB.
- **Knowledge / reasoning:**
  - MMLU-Pro CS 87.5 %, engineering 75.5 %, MMLU security 85 %, IFEval 91.5 % strict (best so far).
  - MMLU college CS only 69 %: 26 of its 28 no-answers are prose answers that explain the statements but never give the option letter. That's a format failure, scored wrong for every model alike.
  - The 38 empty MMLU-Pro engineering answers and 60 GPQA no-answers (thinking on) are the 16k-token budget running out mid-reasoning, as with nemotron.
  - BBH 94.4 % thinking on and off.
  - **GPQA diamond: 65.7 % thinking on, 83.3 % thinking off.** Thinking off is the best GPQA score of any model: with the budget it answers almost everything (2 no-answers) instead of over-deliberating.
- **Thinking off is faster and at least as good here:** S6nt took 3.1 h vs S6 9.4 h.
- **Read:** on capability Flash is clearly the strongest model for this workload: S3 +21 points, S5 12/12 and Polyglot +23 points over qwen36. It runs at ⅓–½ of qwen36's speed (about 5× the agent time per Polyglot trial). Whether that trade is worth it is the main decision for the final analysis. Its persistent attempts to get around the sandbox are worth knowing about if it runs agents with real network access.

### qwen36-fp8 (Qwen3.6-35B-A3B FP8, same model as qwen36 at higher precision): done 2026-10-17, ~27 h

- **Purpose:** does NVFP4 cost qwen36 quality? Answer: **no measurable loss**. The differences go both ways and are within run-to-run noise.

| | qwen36 (NVFP4) | qwen36-fp8 |
|---|---|---|
| Speed c1 / c4 per stream | 109 / 55 tok/s | 49 / 28 tok/s |
| S5 | 9/12 | 7/12 |
| S3 Terminal-Bench | 20/53 (37.7 %) | 17/53 (32.1 %) |
| S4 Polyglot | 97/225 (43.1 %) | 108/225 (48.0 %)* |
| MMLU-Pro CS / eng | 89 / 80 % | 87.5 / 82.5 % |
| MMLU security / college CS | 87 / 96 % | 86 / 94 % |
| IFEval strict | 90.5 % | 88 % |
| BBH on / off | 94.2 / 94.1 % | 94.6 / 94.4 % |
| GPQA on / off | 80.8 / 78.3 % | 76.3 / 78.8 % |

  \*First model run with the web blocked in Polyglot. qwen36 never used the web there, so the two are comparable. Its one attempt to fetch a test file from GitHub was refused.
- **FP8 is half the speed** (more bytes per weight; it also has the MTP head, so that's not the difference).
- **Interim read:** qwen36 NVFP4 is the better Daily choice than its FP8 build. Same quality, twice the speed.

### coder-next-fp8 (Qwen3-Coder-Next FP8, same model as coder-next at higher precision): done 2026-10-18, ~18.5 h

- **Same quality as NVFP4 coder-next within noise; slower under load.**

| | coder-next (NVFP4) | coder-next-fp8 |
|---|---|---|
| Speed c1 / c4 per stream | 37 / 27 tok/s | 38 / 18 tok/s |
| S1 | 14/16 | 15/16 |
| S5 | 6/12 | 7/12 |
| S3 Terminal-Bench | 16/53 (30.2 %) | 13/53 (24.5 %) |
| S4 Polyglot | 101/225 (44.9 %) | 108/225 (48.0 %)* |
| MMLU-Pro CS / eng | 85.5 / 74.5 % | 84.5 / 78.5 % |
| MMLU security / college CS | 76 / 96 % | 86 / 98 % |
| IFEval strict | 78 % | 82 % |
| BBH / GPQA | 91.9 / 75.8 % | 90.3 / 73.2 % |

  \*Web blocked; no fetch attempts at all. coder-next never went online either, so the two are comparable.
- **Interim read:** neither coder-next build is a Daily candidate: both trail qwen36 on S3/S5 and are slower. FP8 doesn't change that.

### devstral-small (Devstral Small 24B FP8, dense, non-thinking): done 2026-10-18 (run split by the postponement)

- **The weakest model on almost everything, and by far the slowest:** 9 tok/s single stream, 9 tok/s per stream at 4 (a dense 24B model at FP8 has to read every weight for each token).
- **Agentic:** S1 15/16 (but 2 h vs 6–18 min for the others); S5 3/12; S3 12/53 (22.6 %); S4 Polyglot 51/225 (22.7 %).
  - Polyglot took 52.6 agent-hours, with 16.6k tool calls and 257M input tokens; many C++/Java tasks ran into the 30-min timeout (78 exceptions).
  - The second half of Polyglot resumed the old job, so the web was not blocked; checked: it never fetched tests or solutions (1 harmless Exercism page).
- **Knowledge / reasoning:** MMLU-Pro CS 73 %, engineering 44 %; MMLU security 76 %, college CS 79 %; IFEval 65.5 % strict; BBH 85.6 %; GPQA 52.5 %. All lowest of the field. No thinking mode, so S6nt was skipped (approved).
- **Interim read:** not a candidate.
