# Run manifest

| run_id | config | param | rep | started | throughput | p95 | errors | active |
|---|---|---|---|---|---|---|---|---|
| SCALE_ramp | SCALE | ramp | 1 | 2026-09-04T12:46:34 | 189.89 rps | 3525.14 ms | 0 (0.0%) | 1-3 |
| FAIL_recovery_c100 | FAIL | kill | 1 | 2026-09-04T12:49:20 | 240.63 rps | 2782.6 ms | 36 (0.103%) | 2-3 |
| ALGO_adaptive_hog_c50_rep1 | ALGO | adaptive+hog | 1 | 2026-09-04T12:50:20 | 281.97 rps | 866.75 ms | 0 (0.0%) | 3-3 |
| ALGO_round_robin_hog_c50_rep1 | ALGO | round_robin+hog | 1 | 2026-09-04T12:51:23 | 277.31 rps | 1284.59 ms | 0 (0.0%) | 3-3 |
| ALGO_least_connections_hog_c50_rep1 | ALGO | least_connections+hog | 1 | 2026-09-04T12:52:27 | 281.26 rps | 1119.54 ms | 0 (0.0%) | 3-3 |
| ALGO_adaptive_hog_c50_rep2 | ALGO | adaptive+hog | 2 | 2026-09-04T12:53:30 | 287.43 rps | 916.81 ms | 0 (0.0%) | 3-3 |
| ALGO_round_robin_hog_c50_rep2 | ALGO | round_robin+hog | 2 | 2026-09-04T12:54:34 | 274.46 rps | 801.13 ms | 0 (0.0%) | 3-3 |
| ALGO_least_connections_hog_c50_rep2 | ALGO | least_connections+hog | 2 | 2026-09-04T12:55:37 | 295.4 rps | 1111.5 ms | 0 (0.0%) | 3-3 |
| ALGO_adaptive_nohog_c50_rep1 | ALGO | adaptive | 1 | 2026-09-04T12:56:35 | 287.51 rps | 663.96 ms | 0 (0.0%) | 3-3 |
| ALGO_round_robin_nohog_c50_rep1 | ALGO | round_robin | 1 | 2026-09-04T12:57:28 | 266.74 rps | 1047.37 ms | 0 (0.0%) | 3-3 |
| L2_c1_rep1 | L2 | 1 | 1 | 2026-09-04T12:58:21 | 44.49 rps | 96.61 ms | 0 (0.0%) | 2-2 |
| L3_c1_rep1 | L3 | 1 | 1 | 2026-09-04T12:59:16 | 43.23 rps | 97.36 ms | 0 (0.0%) | 3-3 |
