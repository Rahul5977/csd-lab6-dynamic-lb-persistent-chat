| Test | Scenario | Sent | Stored in DB | duplicate:true responses | Backends involved | Result |
|---|---|---|---|---|---|---|
| T1 | same id, sequential retries | 20 | **1** | 19 | sys2, sys3, sys4 | PASS  |
| T2 | same id, concurrent storm across backends | 20 | **1** | 19 | sys2, sys3, sys4 | PASS  |
| T3 | send, drop connection, reconnect, re-send | 2 | **1** | 1 | sys3, sys4 | PASS  |
| T4 | Idempotency-Key header | 5 | **1** | 4 | sys4 | PASS  |
| T5 | control: distinct ids | 20 | **20** | 0 | sys3 | PASS  |
| P1 | persistence across DB-service restart | 49 | **49** | 1 | via LB | PASS: count 48→49 across restart, probe readable=True, dup-of-old-id still rejected=True |

Database after the test: 273769 messages, 10 duplicates rejected in total (sqlite (node:sqlite, WAL)).
