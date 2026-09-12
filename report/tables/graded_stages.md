The final graded runs, per stage, as the leaderboard itself recorded them. The static ladder is
delivered in full with no errors; the breakpoint ladder is held to 2 500 concurrent users at a
throughput that stays flat from 500 users upward, which is what bounded dispatch buys.

| board | users | requests | successful | errors | req/s | mean ms |
|---|---|---|---|---|---|---|
| static | 250 | 5000 | 5000 | 0.0 % | 708 | 70 |
| static | 500 | 5000 | 5000 | 0.0 % | 470 | 190 |
| static | 750 | 5000 | 5000 | 0.0 % | 316 | 456 |
| static | 1000 | 5000 | 5000 | 0.0 % | 266 | 689 |
| **static — total** | — | **20000** | **20000** | **0.00 %** | — | **351** |
| breakpoint | 200 | 5000 | 5000 | 0.0 % | 260 | 138 |
| breakpoint | 350 | 5000 | 5000 | 0.0 % | 213 | 351 |
| breakpoint | 500 | 4506 | 4506 | 0.0 % | 173 | 681 |
| breakpoint | 750 | 4959 | 4895 | 1.3 % | 177 | 831 |
| breakpoint | 1000 | 5000 | 4862 | 2.8 % | 174 | 836 |
| breakpoint | 1500 | 5000 | 4787 | 4.3 % | 175 | 894 |
| breakpoint | 2000 | 5000 | 4776 | 4.5 % | 179 | 1335 |
| breakpoint | 2500 | 5000 | 4757 | 4.9 % | 176 | 1739 |
| **breakpoint — total** | — | **39465** | **38583** | **2.23 %** | — | **844** |
