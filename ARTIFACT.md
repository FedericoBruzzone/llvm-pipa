# LLVM Pass Incremental Performance Analysis Artifact

Install on your system the following tools:
- [`hyperfine`](https://github.com/sharkdp/hyperfine) 
- [`perf`](https://github.com/brendangregg/perf-tools) (Linux systems) - [`xctrace`](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/)[^1] (macOS systems)



## Run a quick performance analysis

The following will take around 10 minutes to run and will give you a quick overview of the performance of the LLVM pass incremental performance analysis framework on a subset of three our own benchmarks.

```bash
./scripts/x.sh --quick --config configs/test_benchmarks.toml
```

<!-- ## Setup the Polybench benchmark

```bash
python3 scripts/setup_benchmarks.py --only polybench
``` -->

[^1]: You need to install `xcode` and `xcode command line tools` to get access to `xctrace`. A possible path is to install `xcode` from the App Store and then run `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer` to set the path to the command line tools.

