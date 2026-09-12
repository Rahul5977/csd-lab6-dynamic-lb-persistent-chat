Each change measured on its own, 1 000 concurrent users, 8 000 requests, same cluster and same client.
These three are all on the `/message` path, so they were measured with write-heavy traffic; the feed
work that dominates the evaluation's actual mix is measured separately in §14.4 and §14.6.

| change | what it does | throughput (req/s) | mean response (ms) | errors |
|---|---|---|---|---|
| **baseline** | thread-per-connection balancer, fetch() to the database, one commit per message | 368 | 2602 | 0.1 % |
| **asyncio balancer** | the proxy's I/O layer moved onto an event loop | 461 | 1777 | 1.9 % |
| **keep-alive database client** | http.request over a pooled agent instead of global fetch() | 527 | 1582 | 1.9 % |
| **group commit** | appends arriving in one tick share a transaction | 787 | 1163 | 0.0 % |
| **net** |  | **×2.1** | **−55 %** |  |
