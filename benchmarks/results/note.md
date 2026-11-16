

Recorded results.
pilot deploy     
deployment dep_de9220e550f0fb71: preparing
  HP EliteBook     layers   0-0   head=no  transport=local     endpoint=10.10.150.105:50100    rtt=42.4 ms
  Ubuntu VM        layers   1-1   head=no  transport=local     endpoint=10.10.132.197:50100    rtt=18.5 ms
  Mac Studio       layers   2-23  head=yes transport=local     endpoint=-                      rtt=3.4 ms
  predicted: 71.7 ms/token (13.95 tokens/s), network 62.12 ms, slowest stage dev_8067e945fdd624be
preparing | HP EliteBook: ready 100% | Ubuntu VM: ready 100%
preparing
ready
ready after 8.1 s




pilot bench --n 20 --max-tokens 64                                                                           
  1: completed   3.52 s   60 tokens   17.1 tokens/s end to end
  2: completed   1.25 s   12 tokens    9.6 tokens/s end to end
  3: completed   3.52 s   64 tokens   18.2 tokens/s end to end
  4: completed   1.26 s   20 tokens   15.9 tokens/s end to end
  5: completed   3.52 s   60 tokens   17.0 tokens/s end to end
  6: completed   0.84 s   12 tokens   14.4 tokens/s end to end
  7: completed   3.32 s   64 tokens   19.3 tokens/s end to end
  8: completed   1.46 s   20 tokens   13.7 tokens/s end to end
  9: completed   3.73 s   60 tokens   16.1 tokens/s end to end
 10: completed   0.84 s   12 tokens   14.3 tokens/s end to end
 11: completed   4.38 s   64 tokens   14.6 tokens/s end to end
 12: completed   1.25 s   20 tokens   16.0 tokens/s end to end
 13: completed   3.12 s   60 tokens   19.2 tokens/s end to end
 14: completed   0.83 s   12 tokens   14.4 tokens/s end to end
 15: completed   3.73 s   64 tokens   17.1 tokens/s end to end
 16: completed   1.25 s   20 tokens   16.0 tokens/s end to end
 17: completed   3.10 s   60 tokens   19.3 tokens/s end to end
 18: completed   1.05 s   12 tokens   11.4 tokens/s end to end
 19: completed   3.10 s   64 tokens   20.6 tokens/s end to end
 20: completed   1.46 s   20 tokens   13.7 tokens/s end to end
{
  "requests": 20,
  "completed": 20,
  "median_elapsed_s": 2.281,
  "median_tokens_per_s": 16.02
}
wrote /Users/mtodd/Documents/difa/benchmarks/results/pilot-3machine-20261116T213925Z.json