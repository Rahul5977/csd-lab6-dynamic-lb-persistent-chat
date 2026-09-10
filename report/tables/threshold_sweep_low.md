The same sweep at 10 concurrent clients. At this load the threshold really decides whether the cluster concentrates traffic on one warm backend or spreads it.

| switch threshold T | reps | p50 (ms) | p95 (ms) | throughput (req/s) | busiest backend share | sys1 CPU (%) | backend CPU (%) |
|---|---|---|---|---|---|---|---|
| **0.15** ← deployed | 2 | 100 | 437 | 58.6 | 44 % | 17 | 16 |
| **0.30** | 2 | 144 | 441 | 49.6 | 43 % | 16 | 13 |
| **0.45** | 2 | 277 | 925 | 26.3 | 49 % | 9 | 9 |
| **0.55** | 2 | 226 | 655 | 34.4 | 73 % | 11 | 10 |
| **0.70** | 2 | 218 | 547 | 36.2 | 89 % | 11 | 11 |
| **0.85** | 2 | 181 | 513 | 40.9 | 97 % | 13 | 13 |
| **1.00** | 2 | 188 | 470 | 41.0 | 92 % | 13 | 12 |
