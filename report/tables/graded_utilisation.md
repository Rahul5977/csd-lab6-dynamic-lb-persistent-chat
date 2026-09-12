Sampled once a second from each container's cgroup v2 accounting during a graded run (static ladder
then breakpoint ladder, ~260 s). "Busy" = seconds in which that system was above 20 % CPU. The
throttling column is a 5-second reading taken at the height of the run: scheduling periods (of 50) in
which the container was stopped for exhausting its one-core quota.

| system | role | CPU, mean while busy | CPU, max | throttled periods / 50 | memory, max |
|---|---|---|---|---|---|
| sys1 | load balancer | 58 % | 92 % | **0** | **461 MB** of 512 |
| sys2 | backend | 63 % | 106 % | 30 | 318 MB |
| sys3 | backend + database | **76 %** | 105 % | 26 | 460 MB |
| sys4 | backend | 63 % | 104 % | 29 | 480 MB |
