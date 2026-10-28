from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from spars.core.runner.cores import DEFAULT_SUPPORT_CORES, allocate_cores, set_affinity
from spars.core.runner.worker import RUNNER_THREAD_LIMIT_ENV, find_venv_python, terminate_process


DEFAULT_CORES = 2


async def run_module(
    module_name: str,
    python_path: Path,
    args: Sequence[object] | None = None,
    thread_limit: int = 1,
    env_overrides: dict[str, str] | None = None,
) -> None:
    """Run one outer Python module and wait for it to finish."""
    if thread_limit < 1:
        raise ValueError("thread_limit must be at least 1")

    command = [
        str(python_path),
        "-m",
        module_name,
        *(str(argument) for argument in (args or [])),
    ]

    child_environment = os.environ.copy()
    child_environment[RUNNER_THREAD_LIMIT_ENV] = str(thread_limit)
    if env_overrides:
        child_environment.update(env_overrides)

    print("\n" + "=" * 70, flush=True)
    print(f"Starting: {module_name}", flush=True)
    print(f"Worker concurrency: {thread_limit}", flush=True)
    print(f"Command: {' '.join(command)}", flush=True)
    print("=" * 70, flush=True)

    process = await asyncio.create_subprocess_exec(
        *command,
        env=child_environment,
    )

    try:
        return_code = await process.wait()
    except asyncio.CancelledError:
        await terminate_process(process)
        raise

    if return_code != 0:
        raise RuntimeError(f"{module_name} failed with exit code {return_code}")

    print(f"\nFinished: {module_name}", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SPARS experiments")
    # Unset options fall back to the dashboard's remembered settings, or to
    # the defaults outside the dashboard.
    parser.add_argument(
        "cores",
        metavar="CPUS",
        help="Simulation cores, one per concurrent simulation worker",
        nargs="?",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--support-cores",
        type=int,
        default=None,
        help="Cores for everything other than simulation (logging, plotting)",
    )
    parser.add_argument(
        "--metrics", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--gantt", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--plot-only",
        metavar="RESULT_ROOT",
        help="Plot saved runs without executing experiments",
    )
    args = parser.parse_args()

    if args.cores is not None and args.cores < 1:
        parser.error("cores must be at least 1")
    if args.support_cores is not None and args.support_cores < 1:
        parser.error("support cores must be at least 1")
    return args


def _run_dashboard(args: argparse.Namespace) -> None:
    from spars.core.dashboard.app import run_dashboard
    from spars.core.dashboard.settings import DashboardSettings

    settings = DashboardSettings.load()
    for name, value in (
        ("simulation_cores", args.cores),
        ("support_cores", args.support_cores),
        ("metrics", args.metrics),
        ("gantt", args.gantt),
    ):
        if value is not None:
            setattr(settings, name, value)
    run_dashboard(settings)


async def _run_all(args: argparse.Namespace) -> None:
    """Without a terminal: run every definition once, as before."""
    cores = args.cores or DEFAULT_CORES
    support_cores = args.support_cores or DEFAULT_SUPPORT_CORES
    executor_args: list[object] = [
        cores,
        "--support-cores",
        support_cores,
        "--metrics" if args.metrics else "--no-metrics",
        "--gantt" if args.gantt else "--no-gantt",
    ]
    allocation = allocate_cores(cores, support_cores)
    # The CLI itself is Support work; the executor reuses this allocation.
    set_affinity(allocation.support)
    python_path = find_venv_python()

    print("Master script configuration:", flush=True)
    print(
        f"  Simulation cores: {[list(core) for core in allocation.simulation]}",
        flush=True,
    )
    print(f"  Support cores: {list(allocation.support)}", flush=True)
    print(f"  Python: {python_path.resolve()}", flush=True)

    await run_module(
        module_name="spars.core.experiments.executor",
        python_path=python_path,
        args=executor_args,
        thread_limit=cores,
        env_overrides=allocation.to_environment(),
    )

    print("\nAll scripts completed successfully.", flush=True)


def main() -> None:
    args = _parse_args()
    if args.plot_only is not None:
        from spars.core.analysis.automatic import generate_all_plots, load_saved_runs

        generate_all_plots(
            load_saved_runs(args.plot_only),
            generate_metrics=True if args.metrics is None else args.metrics,
            generate_gantt=bool(args.gantt),
        )
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        _run_dashboard(args)
        return
    asyncio.run(_run_all(args))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        print(f"\nError: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nExecution cancelled.", file=sys.stderr, flush=True)
        raise SystemExit(130)
