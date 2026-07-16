"""Process-wide low-memory defaults loaded automatically by Python's site module."""
from __future__ import annotations

import os

# The bot processes small data frames serially. Extra BLAS threads only add native
# thread stacks and CPU contention on Railway without improving signal latency.
for variable in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[variable] = "1"

os.environ["MALLOC_ARENA_MAX"] = "2"
