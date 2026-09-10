Mean CPU of each of the four systems while the offered load rises (3 backends). Each system is a container with a one-CPU quota, so 100 % is its whole allowance.

| backends | users | throughput (req/s) | sys1 — load balancer | sys2 — backend | sys3 — backend + DB | sys4 — backend |
|---|---|---|---|---|---|---|
| 3 | 1 | 16.7 | 4 % | 9 % | 6 % | 3 % |
| 3 | 5 | 82.0 | 17 % | 8 % | 35 % | 21 % |
| 3 | 10 | 126.3 | 36 % | 21 % | 43 % | 33 % |
| 3 | 25 | 203.1 | 68 % | 42 % | 52 % | 39 % |
| 3 | 50 | 258.6 | 73 % | 44 % | 58 % | 48 % |
| 3 | 100 | 225.4 | 72 % | 43 % | 61 % | 48 % |
| 3 | 200 | 229.5 | 71 % | 49 % | 64 % | 48 % |
