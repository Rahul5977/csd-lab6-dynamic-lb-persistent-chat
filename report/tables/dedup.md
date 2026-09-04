| Test | Scenario | Sent | Stored in DB | duplicate:true responses | Backends involved | Result |
|---|---|---|---|---|---|---|
| T1 | same id, sequential retries | 20 | **1** | 19 | sys2, sys3, sys4 | PASS  |
| T2 | same id, concurrent storm across backends | 20 | **1** | 19 | sys2, sys3, sys4 | PASS  |
| T3 | send, drop connection, reconnect, re-send | 2 | **1** | 1 | sys2, sys4 | PASS  |
| T4 | Idempotency-Key header | 5 | **1** | 4 | sys2 | PASS  |
| T5 | control: distinct ids | 20 | **20** | 0 | sys3 | PASS  |

Database after the test: 112484 messages, 4 duplicates rejected in total (sqlite (node:sqlite, WAL)).
