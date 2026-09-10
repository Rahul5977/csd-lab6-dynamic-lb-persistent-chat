The evaluation's own ladders, reproduced locally, before and after the four changes. "before" already includes the asyncio balancer; the baseline could not finish these ladders without double-digit error rates.

| board |  | users | requests | successful | errors | mean ms | req/s |
|---|---|---|---|---|---|---|---|
| static | before | 250 | 5250 | 5250 | 0.0 % | 547 | 448 |
| static | before | 500 | 5500 | 5500 | 0.0 % | 977 | 490 |
| static | before | 750 | 5750 | 5750 | 0.0 % | 1400 | 488 |
| static | before | 1000 | 6000 | 5989 | 0.2 % | 1859 | 485 |
| **static — before** | **total** | — | 22500 | **22489** | **0.05 %** | **1220** | 490 |
| static | after | 250 | 5250 | 5250 | 0.0 % | 240 | 998 |
| static | after | 500 | 5500 | 5500 | 0.0 % | 508 | 925 |
| static | after | 750 | 5750 | 5750 | 0.0 % | 582 | 1148 |
| static | after | 1000 | 6000 | 6000 | 0.0 % | 818 | 1068 |
| **static — after** | **total** | — | 22500 | **22500** | **0.00 %** | **547** | 1148 |
| breakpoint | before | 200 | 5200 | 5200 | 0.0 % | 415 | 472 |
| breakpoint | before | 350 | 5350 | 5350 | 0.0 % | 684 | 495 |
| breakpoint | before | 500 | 5500 | 5500 | 0.0 % | 970 | 495 |
| breakpoint | before | 750 | 5750 | 5750 | 0.0 % | 1648 | 426 |
| breakpoint | before | 1000 | 6000 | 5864 | 2.3 % | 1784 | 441 |
| breakpoint | before | 1500 | 6500 | 5647 | 13.1 % | 2452 | 347 |
| **breakpoint — before** | **total** | — | 34300 | **33311** | **2.88 %** | **1349** | 495 |
| breakpoint | after | 200 | 5200 | 5200 | 0.0 % | 197 | 986 |
| breakpoint | after | 350 | 5350 | 5350 | 0.0 % | 294 | 1084 |
| breakpoint | after | 500 | 5500 | 5500 | 0.0 % | 389 | 1207 |
| breakpoint | after | 750 | 5750 | 5750 | 0.0 % | 587 | 1107 |
| breakpoint | after | 1000 | 6000 | 6000 | 0.0 % | 758 | 1127 |
| breakpoint | after | 1500 | 6500 | 6500 | 0.0 % | 1023 | 1160 |
| **breakpoint — after** | **total** | — | 34300 | **34300** | **0.00 %** | **563** | 1207 |
