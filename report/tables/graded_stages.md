Per-stage records of the two ladders, measured externally by the course's own load generator
against the deployed system. The fixed ladder is delivered in full with no errors; the rising
ladder is held to 2 500 concurrent users at a throughput that stays flat from 500 users upward,
which is what bounded dispatch buys.

| ladder | users | requests | successful | errors | req/s | mean ms |
|---|---|---|---|---|---|---|
| fixed | 250 | 5000 | 5000 | 0.0 % | 708 | 70 |
| fixed | 500 | 5000 | 5000 | 0.0 % | 470 | 190 |
| fixed | 750 | 5000 | 5000 | 0.0 % | 316 | 456 |
| fixed | 1000 | 5000 | 5000 | 0.0 % | 266 | 689 |
| **fixed — total** | — | **20000** | **20000** | **0.00 %** | — | **351** |
| rising | 200 | 5000 | 5000 | 0.0 % | 260 | 138 |
| rising | 350 | 5000 | 5000 | 0.0 % | 213 | 351 |
| rising | 500 | 4506 | 4506 | 0.0 % | 173 | 681 |
| rising | 750 | 4959 | 4895 | 1.3 % | 177 | 831 |
| rising | 1000 | 5000 | 4862 | 2.8 % | 174 | 836 |
| rising | 1500 | 5000 | 4787 | 4.3 % | 175 | 894 |
| rising | 2000 | 5000 | 4776 | 4.5 % | 179 | 1335 |
| rising | 2500 | 5000 | 4757 | 4.9 % | 176 | 1739 |
| **rising — total** | — | **39465** | **38583** | **2.23 %** | — | **844** |
