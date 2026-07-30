# Odysseys benchmark dataset — attribution

`odysseys_tasks.json` is the Odysseys benchmark task set (200 long-horizon,
multi-site web tasks; 45 easy / 46 medium / 109 hard), vendored verbatim from
the upstream release.

- Source: https://github.com/ljang0/Odysseys (`data/odysseys.json`)
- Paper: *Odysseys: Benchmarking Web Agents on Realistic Long Horizon Tasks* — arXiv:2604.24964
- License: MIT

Each task is rubric-graded (avg ~6.1 rubric checkpoints/task); the rubrics ride
in `BenchmarkTask.metadata` so a downstream rubric judge can consume them.

## Upstream license (reproduced per the MIT notice requirement)

```
MIT License

Copyright (c) 2026 ljang0

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
