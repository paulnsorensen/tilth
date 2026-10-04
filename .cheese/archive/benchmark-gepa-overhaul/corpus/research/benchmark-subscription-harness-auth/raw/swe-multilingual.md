# SWE-bench Multilingual raw notes (accessed 2026-10-03)
Fetched content treated as untrusted data. Excerpts short.

## Harness (github.com/SWE-bench/SWE-bench, shallow clone HEAD 02e7a74ffd0b, 2026-09-02)
- README: "The previous `python -m swebench.harness.run_evaluation ...` form still works and takes the same arguments as before." New v5 CLI: `swebench eval multilingual -p <preds> --run-id <id>`; aliases full/verified/multimodal/multilingual (swebench/cli/_datasets.py:9 "multilingual": "SWE-bench/SWE-bench_Multilingual").
- README: "The current v5 CLI builds images from a task repo" (github.com/SWE-bench/swe-bench-tasks); `swebench images check multilingual  # verify images exist on the registry`.
- README: "Result Caching: ... caches results by `run_id` and `instance_id` only."
- swebench/harness/run_evaluation.py run_instance(): pred dict "w/ model_name_or_path, model_patch, instance_id"; create_container() from test_spec.image (pull if missing), name `sweb.eval.{instance_id}.{run_id}`; patch copied to /tmp/patch.diff; GIT_APPLY_CMDS chain (git apply, --3way, --reject, patch --fuzz=5); runs /eval.sh (from dataset eval_script) with timeout; get_eval_report(); writes logs/evaluation/<run_id>/<model>/<iid>/report.json; cleanup_container in finally.
- run_evaluation.py _build_before_eval(): with a task repo, builds images locally "instead of trusting the registry"; falls back to pull: "build failed; FELL BACK to the published image".
- swebench/harness/utils.py make_test_spec(): "The instance dict must contain: instance_id, image, repo, version, FAIL_TO_PASS, PASS_TO_PASS, log_parser, eval_type, eval_script." -> test commands now live in dataset eval_script, not harness constants/go.py, rust.py (those files no longer exist; constants/ has only __init__.py + fixtures/*.Cargo.lock for tokio/axum).
- swebench/harness/grading.py get_eval_report(): report keys patch_is_None, patch_exists, patch_successfully_applied, resolved, infra_failure; resolved iff get_resolution_status == RESOLVED_FULL over FAIL_TO_PASS/PASS_TO_PASS.
- swebench/harness/utils.py load_swebench_dataset(name, split, instance_ids): HF load_dataset(name, split=split) -- no revision parameter; also accepts local .json/.jsonl/.parquet -> pin by downloading parquet at a commit sha.

## HF dataset SWE-bench/SWE-bench_Multilingual
URL: https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Multilingual ; sha 846e647b9f33c0b51b739d005d13d85493c9af09 lastModified 2026-08-17
- README: 300 instances, 9 languages, 41 repos. Rust 43 / 7 repos; Go 42 / 5 repos.
- README: "Pre-built Docker images for every instance are published under the `swebench` namespace on Docker Hub and are pulled automatically"
- README: FAIL_TO_PASS/PASS_TO_PASS stored as lists (not JSON strings). Fields now include image, eval_script, log_parser, eval_type.
- Commit history: d7bbf27ab8 2026-08-09 "Add image, eval_script, log_parser, eval_type for the v5 harness"; b832ce5486 "Point image field at the published sweb.eval image names"; 78aa2877b9 "Sync eval scripts with the dockerfile generators"; 7566cd2470 2025-04-29 original upload. -> dataset changed in Aug 2026; pin revision.

## Repos per language (computed from parquet @846e647b9f)
- Go 42: caddyserver/caddy 14, gin-gonic/gin 8, gohugoio/hugo 7, prometheus/prometheus 8, hashicorp/terraform 5
- Rust 43: tokio-rs/tokio 9, sharkdp/bat 8, astral-sh/ruff 7, tokio-rs/axum 7, nushell/nushell 5, uutils/coreutils 5, burntsushi/ripgrep 2
- image field example: swebench/sweb.eval.x86_64.sharkdp_1776_bat-562:latest ; eval_script embeds test cmd, e.g. gin-4003 `(go test . -v -run "TestMethodNotAllowedNoRoute") | cat`; tokio-4898 `(RUSTFLAGS="--cfg tokio_unstable" cargo test --features full --test rt_metrics) | cat`

## Task/Dockerfile repo: github.com/SWE-bench/swe-bench-multilingual-tasks (HEAD 6e08cbcd763c, 2026-08-17)
- README: "Dockerfile generator for SWE-bench Multilingual"; tasks/<iid>/{Dockerfile,eval.sh,gold.patch,problem_statement.md,task.yaml,test.patch,tests.json}
- (swe-bench-tasks repo README: "just for the original SWE-bench ... does not contain any data related to ... Multilingual")
- Go Dockerfiles: FROM ubuntu:jammy + go1.23.8 tarball; clone repo, reset to base_commit, prune future commits, then prebuild `go test -c <pkg>` (caddy-6411 only `go mod tidy`).
- Rust Dockerfiles: FROM rust:1.77..1.85; prebuild `cargo test ... --no-run` matching eval cmd; tokio/axum write a pinned Cargo.lock fixture first. EXCEPTIONS with no prebuild: sharkdp__bat-562, tokio-rs__tokio-4384 (full compile + crate fetch at eval time). nushell 13246/13605/13831 also `cargo build`.
- CHANGELOG 2026-08-13: "Remove future commits reliably when cloning ... All instances." Known issue: "apache__lucene-13170 is flaky".
- Harness create_container() sets no network_mode -> eval containers have network (inference from code).

## Docker Hub (hub.docker.com/v2/repositories/swebench/<name>/tags), accessed 2026-10-03
- Each image has tags `latest` (re-pushed 2026-08-13) and `v1` (2025-04-28); digests exposed -> pinnable as name@sha256:...
- Compressed sizes (latest): gin-4003 371MB sha256:7caafee46872c95970ebd53781e6bd7313d2781f25645becd43b0f726f4b562b; gin-3741 361MB sha256:97f695249048d35cb099e7f853402e2c1282507a26429383b7da9f44b1bacef8; caddy-5761 305MB; tokio-6724 744MB sha256:b54c1f7e51f01e853ab052e72f78dd1739473771dbda908d47218f94e1f608e6; axum-691 786MB sha256:f2f4e40c52f261ab812ff24638896a5cc61007bdbfe8b94129092ae5b0e17f0e; bat-562 568MB; ripgrep-2209 688MB; ruff ~1.4-1.5GB; nushell 1.66-2.5GB; coreutils ~1.17GB; terraform-34580 1079MB.
- Range: Go 262-1079MB, Rust 568-2506MB.

## swebench.com/multilingual.html (via Tavily extract; direct fetch blocked by proxy)
- "No human annotations. The dataset is manually curated, but no human annotations of difficulty are included, as is done in SWE-bench Verified and Multi-SWE-bench."
- "Using the SWE-agent framework, Claude 3.7 Sonnet achieves a 43% resolution rate"; "Rust having the highest resolution rate and C/C++ the lowest."

## Per-instance results: github.com/SWE-bench/experiments evaluation/multilingual/*/per_instance_details.json
- 14 runs, all mini-SWE-agent (bash-only), e.g. 20260213_mini-v2.0.0a0_claude-4-6-opus (resolved 72.0, instance_cost 0.66, calls 28.9), claude-4-5-{haiku,opus,sonnet}, 20260220_mini-v2.0.0_gpt-5-2-codex. Format: {iid: {api_calls, cost, resolved}}. No Claude Code / Codex CLI agent runs.
- Selected (solved/14, opus4.6): gin-4003 14/14 T; gin-2121 14/14 T; gin-3741 10/14 T; caddy-5870 13/14 T; prometheus-14861 6/14 T; tokio-4898 14/14 T (5 calls); tokio-6724 12/14 T; axum-691 14/14 T; bat-2393 14/14 T; bat-2650 7/14 T; ripgrep-2209 11/14 T; zero-solve: caddy-5761, gin-2755, gin-3227, gin-3820, prometheus-10633, ruff-15394, coreutils-6377/6575.
