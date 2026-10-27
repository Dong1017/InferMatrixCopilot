"""Match the target repo's simple CPU jobs' single-card Buildkite allocation.

The parent campaign holds four cards for later GPU jobs. Exposing all four
to a Simple CPU job enables extra GPU parity tests beyond its l4_1 mirror.
The temporary target pytest entrypoint calls this helper; GPU and mixed
marker expressions retain their full allocation.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence


def cpu_ci_env(argv: Sequence[str], env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    expression = None
    for index, arg in enumerate(argv):
        if arg in ("-m", "--markexpr") and index + 1 < len(argv):
            expression = argv[index + 1]
        elif arg.startswith(("-m=", "--markexpr=")):
            expression = arg.split("=", 1)[1]
    if expression is None or not re.fullmatch(
        r"\s*core_model\s+and\s+cpu\s*", expression
    ):
        return result
    devices = [
        value.strip()
        for value in result.get("CUDA_VISIBLE_DEVICES", "").split(",")
        if value.strip()
    ]
    if len(devices) > 1:
        result["CUDA_VISIBLE_DEVICES"] = devices[0]
    return result


def main(target_python: str) -> None:
    argv = sys.argv[1:]
    env = cpu_ci_env(argv, os.environ)
    if env.get("CUDA_VISIBLE_DEVICES") != os.environ.get("CUDA_VISIBLE_DEVICES"):
        print(
            "[CPU CI resources] exposing one card to match the l4_1 mirror", flush=True
        )
    os.execve(target_python, [target_python, "-m", "pytest", *argv], env)
