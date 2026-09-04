| Phase | Window (s) | Users | Active backends | Throughput (req/s) | p50 ms | p95 ms | Errors | Share sys2 / sys3 / sys4 |
|---|---|---|---|---|---|---|---|---|
| healthy, 3 backends | 5–29 | 100 | 3 | **280.4** | 125 | 2357 | 0 | 31% / 33% / 35% |
| sys3 killed → detection window | 29–39 | 100 | 3 | **250.0** | 88 | 2737 | 36 | 34% / 26% / 39% |
| 2 backends carry the load | 39–90 | 100 | 2 | **184.1** | 94 | 4421 | 0 | 47% / 0% / 53% |
| sys3 restarted → re-admitted | 90–100 | 100 | 3 | **225.0** | 182 | 1935 | 0 | 32% / 30% / 39% |
| 3 backends again | 100–150 | 100 | 3 | **279.8** | 96 | 2610 | 0 | 33% / 33% / 34% |

Whole run: 34927 requests, 36 failed (0.103 %), 2 sends retried with the same id, 0 of those answered `duplicate:true` (stored once).
