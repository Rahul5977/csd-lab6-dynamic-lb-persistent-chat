| Scenario | Configuration | Throughput (req/s) | p50 ms | p95 ms | Failed | Active backends |
|---|---|---|---|---|---|---|
| L: 100 users, closed loop | 1 backend(s) | **103.06** | 110.51 | 8976.44 | 0 (0.0 %) | 1-1 |
| L: 100 users, closed loop | 2 backend(s) | **170.63** | 168.87 | 4402.06 | 0 (0.0 %) | 2-2 |
| L: 100 users, closed loop | 3 backend(s) | **269.4** | 126.6 | 2587.86 | 0 (0.0 %) | 3-3 |
| L: 200 users, closed loop | 1 backend(s) | **84.85** | 772.25 | 7598.55 | 0 (0.0 %) | 1-1 |
| L: 200 users, closed loop | 2 backend(s) | **189.49** | 260.11 | 7855.05 | 0 (0.0 %) | 2-2 |
| L: 200 users, closed loop | 3 backend(s) | **267.93** | 216.81 | 5716.81 | 0 (0.0 %) | 3-3 |
| O: 200.2 req/s actually offered | 1 backend(s) | **118.64** achieved | 715.03 | 7422.47 | 2038 + 797 dropped | 1-1 |
| O: 205.5 req/s actually offered | 2 backend(s) | **205.48** achieved | 283.48 | 4488.56 | 0 + 0 dropped | 2-2 |
| O: 220.7 req/s actually offered | 3 backend(s) | **220.68** achieved | 64.08 | 267.6 | 0 + 0 dropped | 3-3 |
| SCALE phase A: 1 backend, 25 users | 1 backend(s) | **95.5** | 75 | 2089 | — | 1 |
| SCALE phase B: 1 backend, 50 users | 1 backend(s) | **105.2** | 75 | 4482 | — | 1 |
| SCALE phase C: 1 backend, 100 users (saturated) | 1 backend(s) | **98.6** | 61 | 9028 | — | 1 |
| SCALE phase D: sys3 added → 2 backends | 2 backend(s) | **189.5** | 100 | 4172 | — | 2 |
| SCALE phase E: sys4 added → 3 backends | 3 backend(s) | **285.2** | 102 | 2432 | — | 3 |
| SCALE phase F: 3 backends, 200 users | 3 backend(s) | **291.1** | 133 | 5710 | — | 3 |
| FAIL: 100 users, sys3 killed + restarted | 3 → 2 → 3 backends | **240.63** (whole run) | 101.89 | 2782.6 | 36 (0.103 %) | 2–3 |
