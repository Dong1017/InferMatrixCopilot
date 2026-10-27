"""The CPU mirror must not narrow CUDA or mixed-hardware test jobs."""

import pytest

from infermatrix_copilot.testing.cpu_ci_resources import cpu_ci_env


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-m", "core_model and cpu"], "4"),
        (["--markexpr=core_model and cpu"], "4"),
        (["-m=core_model and cpu"], "4"),
        (["-m", "core_model and cuda and cards_4"], "4,5,6,7"),
        (["-m", "core_model and (cpu or cuda)"], "4,5,6,7"),
        (["-m", "core_model and cpu and cuda"], "4,5,6,7"),
        (["--collect-only"], "4,5,6,7"),
    ],
)
def test_declared_cpu_mirror_keeps_gpu_jobs_and_cli_args(args, expected):
    env = {"CUDA_VISIBLE_DEVICES": "4,5,6,7", "OMP_NUM_THREADS": "4"}
    original_args = list(args)
    adjusted = cpu_ci_env(args, env)
    assert adjusted["CUDA_VISIBLE_DEVICES"] == expected
    assert adjusted["OMP_NUM_THREADS"] == "4"
    assert env["CUDA_VISIBLE_DEVICES"] == "4,5,6,7"
    assert args == original_args


@pytest.mark.parametrize("selection", [None, "", "-1", "GPU-example"])
def test_existing_single_device_or_cpu_only_selection_is_preserved(selection):
    env = {} if selection is None else {"CUDA_VISIBLE_DEVICES": selection}
    assert cpu_ci_env(["-m", "core_model and cpu"], env) == env
