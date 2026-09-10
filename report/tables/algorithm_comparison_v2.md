The chosen threshold rule against the classic algorithms, same load, same cluster.

| algorithm | reps | throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | errors (%) | busiest backend | backend CPU spread | traffic split |
|---|---|---|---|---|---|---|---|---|---|
| **threshold** | 2 | 260.0 | 184 | 447 | 565 | 0.00 | 38 % | 17 pp | sys2 35% / sys3 28% / sys4 37% |
| **least_connections** | 2 | 240.8 | 197 | 471 | 620 | 0.00 | 34 % | 19 pp | sys2 32% / sys3 34% / sys4 34% |
| **adaptive** | 2 | 233.9 | 202 | 489 | 679 | 0.00 | 34 % | 18 pp | sys2 34% / sys3 32% / sys4 34% |
| **round_robin** | 2 | 220.6 | 224 | 494 | 646 | 0.00 | 33 % | 21 pp | sys2 33% / sys3 33% / sys4 33% |
