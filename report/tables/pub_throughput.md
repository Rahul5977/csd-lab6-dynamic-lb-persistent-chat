Open-loop throughput: Poisson arrivals at a fixed offered load, independent of how slow the answers get.

| backends | offered load (req/s) | achieved throughput (req/s) | p95 (ms) | errors (%) | arrivals dropped |
|---|---|---|---|---|---|
| 1 | 50 | 46.8 | 214 | 0.00 | 0 |
| 1 | 100 | 84.6 | 456 | 0.00 | 0 |
| 1 | 200 | 130.5 | 8667 | 1.35 | 0 |
| 1 | 300 | 114.5 | 20724 | 44.50 | 2941 |
| 1 | 400 | 106.9 | 19495 | 61.53 | 5971 |
| 3 | 50 | 47.0 | 222 | 0.00 | 0 |
| 3 | 100 | 84.0 | 323 | 0.00 | 0 |
| 3 | 200 | 153.1 | 3096 | 0.00 | 0 |
| 3 | 300 | 115.9 | 20450 | 42.05 | 2606 |
| 3 | 400 | 111.0 | 18842 | 60.25 | 5806 |
