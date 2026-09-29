# Stopped Sonnet 5 hybrid comparison

The user stops this schedule after finding unequal file-tool restrictions.
Four outcomes are recorded. The fifth cell is interrupted and has no final result.
Do not merge these rows into the strict restart.

Source: benchmark/results/benchmark_20260928_191916_sonnet5.jsonl.
Raw streams: benchmark/results/streams/20260928_191916/.
The task is gin_edit_render_context. Model is claude-sonnet-5, effort high.

| Repetition | Arm | Correct | Agent seconds | Processed input | Native cost USD |
|---|---|---|---:|---:|---:|
| 0 | tilth hybrid | yes | 451.688 | 6629632 | 2.0283256 |
| 0 | native | yes | 511.139 | 5243847 | 1.6762216 |
| 0 | WOZCODE | yes | 351.844 | 2678547 | 1.1013938 |
| 1 | native | no: timeout | >600 | unavailable | unavailable |
| 1 | WOZCODE | interrupted by user | unavailable | unavailable | unavailable |

Processed input includes cache reads. Missing timeout or interrupted usage is not zero.
Costs exclude setup probes. Completed-cell cost is not the total spend of the stopped schedule.

## Directional signal

In the single completed triplet, WOZCODE uses 31.16% less agent time, 48.92% less processed input, and 34.29% less cost than native.
Hybrid tilth uses 11.63% less time, 26.43% more processed input, and 21.01% more cost than native.
All three pass the same trusted and candidate tests.
The second native run times out; do not omit it from the observed outcomes.

This supports further WOZCODE testing. It does not establish a reliable or general performance win.
The tilth arm permits native file tools and uses 47 native Edit calls in its first run.
The WOZCODE agent disables native file tools, but unrestricted Bash remains available.
The asymmetry prevents a strict file-tool comparison.

## Restart decision

The user authorizes a fresh 15-cell schedule with strict file tools.
Baseline uses native file tools. Tilth and WOZCODE use only their MCP tools for source work.
All arms restrict Bash to Go tests, builds, vet, and formatting.
The task, model, high effort, five repetitions, timeout, budget, and shuffle seed remain unchanged.
No benchmark process from this schedule remains running after the stop.
