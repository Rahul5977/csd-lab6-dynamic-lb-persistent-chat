| Phase | Window (s) | Users | Active backends | Throughput (req/s) | p50 ms | p95 ms | Errors | Share sys2 / sys3 / sys4 |
|---|---|---|---|---|---|---|---|---|
| A: 1 backend, 25 users | 5–40 | 25 | 1 | **95.5** | 75 | 2089 | 0 | 100% / 0% / 0% |
| B: 1 backend, 50 users | 45–80 | 50 | 1 | **105.2** | 75 | 4482 | 0 | 100% / 0% / 0% |
| C: 1 backend, 100 users (saturated) | 85–128 | 100 | 1 | **98.6** | 61 | 9028 | 0 | 100% / 0% / 0% |
| D: sys3 added → 2 backends | 136–184 | 100 | 2 | **189.5** | 100 | 4172 | 0 | 52% / 47% / 1% |
| E: sys4 added → 3 backends | 192–260 | 100 | 3 | **285.2** | 102 | 2432 | 0 | 34% / 34% / 32% |
| F: 3 backends, 200 users | 265–300 | 200 | 3 | **291.1** | 133 | 5710 | 0 | 34% / 33% / 33% |
