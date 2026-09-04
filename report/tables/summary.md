| Scenario | Configuration | Throughput (req/s) | p50 ms | p95 ms | Failed | Active backends |
|---|---|---|---|---|---|---|
| SCALE phase A: 1 backend, 25 users | 1 backend(s) | **95.5** | 75 | 2089 | — | 1 |
| SCALE phase B: 1 backend, 50 users | 1 backend(s) | **105.2** | 75 | 4482 | — | 1 |
| SCALE phase C: 1 backend, 100 users (saturated) | 1 backend(s) | **98.6** | 61 | 9028 | — | 1 |
| SCALE phase D: sys3 added → 2 backends | 2 backend(s) | **189.5** | 100 | 4172 | — | 2 |
| SCALE phase E: sys4 added → 3 backends | 3 backend(s) | **285.2** | 102 | 2432 | — | 3 |
| SCALE phase F: 3 backends, 200 users | 3 backend(s) | **291.1** | 133 | 5710 | — | 3 |
| ALGO: 50 users, sys3 CPU-loaded | adaptive | **284.7** | 89.9 | 891.78 | 0 | share sys3 = 34.1 % |
| ALGO: 50 users, sys3 CPU-loaded | round_robin | **275.88** | 62.08 | 1042.86 | 0 | share sys3 = 33.3 % |
| ALGO: 50 users, sys3 CPU-loaded | least_connections | **288.33** | 80.64 | 1115.52 | 0 | share sys3 = 33.3 % |
| FAIL: 100 users, sys3 killed + restarted | 3 → 2 → 3 backends | **240.63** (whole run) | 101.89 | 2782.6 | 36 (0.103 %) | 2–3 |
