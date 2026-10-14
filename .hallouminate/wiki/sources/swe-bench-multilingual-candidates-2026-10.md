# SWE-bench Multilingual candidates for the GEPA panel

The GEPA panel uses two SWE-bench Multilingual tasks: `gin-gonic__gin-3741` (dev) and `sharkdp__bat-2650` (test) (`benchmark/panels/gepa-v1.json:60,66`).
The dataset is pinned to revision `846e647b9f33c0b51b739d005d13d85493c9af09`, because the dataset changed in August 2026 (`benchmark/external/data.py:17`).
The solve counts below come from mini-SWE-agent runs, not from Claude Code or Codex runs. Use them only as a difficulty signal.

Canonical source: `raw/swe-multilingual.md` in the PR #312 session archive, accessed 2026-10-03 ([raw note at the PR #312 head](https://github.com/paulnsorensen/tilth/blob/9e84659b2d27715a0d995b30a760213cefd01096/.cheese/archive/benchmark-gepa-overhaul/corpus/research/benchmark-subscription-harness-auth/raw/swe-multilingual.md)). PR #312 closes unmerged; its head `9e84659` stays reachable through `refs/pull/312/head`.

## Dataset pin

- Hugging Face `SWE-bench/SWE-bench_Multilingual` at sha `846e647b9f33c0b51b739d005d13d85493c9af09` (lastModified 2026-08-17) holds 300 instances in 9 languages from 41 repos. Rust has 43 instances from 7 repos. Go has 42 instances from 5 repos. Source: https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Multilingual.
- The August 2026 commits added the v5 harness fields `image`, `eval_script`, `log_parser`, and `eval_type`. An unpinned load therefore returns different rows than the 2025 upload.
- `load_swebench_dataset(name, split, instance_ids)` calls `load_dataset(name, split=split)` and has no revision parameter (`swebench/harness/utils.py`). To pin, download the parquet at a commit sha and pass the local file.
- The upstream result cache "caches results by `run_id` and `instance_id` only" (SWE-bench README). A new dataset revision or image under an old `run_id` can return a stale result, so use a fresh `run_id` per change.

## Solve counts

The counts are out of 14 runs in `github.com/SWE-bench/experiments` `evaluation/multilingual/*/per_instance_details.json`. All 14 runs are mini-SWE-agent (bash only). None is a Claude Code or Codex CLI run.

| Instance | Solved / 14 | Panel role |
|---|---|---|
| `gin-gonic__gin-3741` | 10 | primary, dev |
| `prometheus__prometheus-14861` | 6 | candidate, not used |
| `sharkdp__bat-2650` | 7 | primary, test |
| `tokio-rs__tokio-6724` | 12 | candidate, not used |

The local gin fixture already holds the gin-3741 fix, so an external gin-3741 cell must not read the local clones. See [Benchmark GEPA overhaul gotchas](../benchmark-gepa-gotchas.md).

## Images

Image sizes and digests were captured from Docker Hub on 2026-10-03. Each image has a `latest` tag (re-pushed 2026-08-13) and a `v1` tag (2025-04-28). Pin by digest, not by tag.

| Image | Compressed size | Digest |
|---|---|---|
| gin-3741 | 361 MB | `sha256:97f695249048d35cb099e7f853402e2c1282507a26429383b7da9f44b1bacef8` |
| tokio-6724 | 744 MB | `sha256:b54c1f7e51f01e853ab052e72f78dd1739473771dbda908d47218f94e1f608e6` |

The archive did not capture sizes or digests for prometheus-14861 or bat-2650. Go images range from 262 MB to 1079 MB, and Rust images range from 568 MB to 2506 MB.

## Related

- [Running FeatureBench and SWE-bench Multilingual on subscription auth](benchmark-subscription-auth-2026-10.md)
- [ADR: benchmark GEPA overhaul](../adr/benchmark-gepa-overhaul.md)

_Source: PR #312 session archive `raw/swe-multilingual.md` (head `9e84659`), accessed 2026-10-03; repo cites verified at the layer 6 head of PR #319 · Updated: 2026-10-13_
