# Run manifest

| run_id | config | param | rep | started | throughput | p95 | errors | active |
|---|---|---|---|---|---|---|---|---|
| SCALE_ramp | SCALE | ramp | 1 | 2026-09-04T12:46:34 | 189.89 rps | 3525.14 ms | 0 (0.0%) | 1-3 |
| FAIL_recovery_c100 | FAIL | kill | 1 | 2026-09-04T12:49:20 | 240.63 rps | 2782.6 ms | 36 (0.103%) | 2-3 |
| ALGO_adaptive_hog_c50_rep1 | ALGO | adaptive+hog | 1 | 2026-09-04T12:50:20 | 281.97 rps | 866.75 ms | 0 (0.0%) | 3-3 |
| ALGO_round_robin_hog_c50_rep1 | ALGO | round_robin+hog | 1 | 2026-09-04T12:51:23 | 277.31 rps | 1284.59 ms | 0 (0.0%) | 3-3 |
| ALGO_least_connections_hog_c50_rep1 | ALGO | least_connections+hog | 1 | 2026-09-04T12:52:27 | 281.26 rps | 1119.54 ms | 0 (0.0%) | 3-3 |
| ALGO_adaptive_hog_c50_rep2 | ALGO | adaptive+hog | 2 | 2026-09-04T12:53:30 | 287.43 rps | 916.81 ms | 0 (0.0%) | 3-3 |
