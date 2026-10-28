# SPARS Simulator

SPARS is an event-driven simulator for job scheduling and node power management in high-performance computing (HPC) systems.

SPARS can:

- generate synthetic workloads and platforms, or convert real SWF traces;
- run schedulers such as FCFS, EASY backfilling and SNF;
- model active, sleeping, switching-on and switching-off nodes, and the power managers that switch them;
- train or evaluate a reinforcement-learning (RL) power manager; and
- produce job, node, energy and waiting-time logs, metrics and Gantt charts.

## Requirements

- Python 3.10 or newer (3.11 or 3.12 recommended)
- Linux, macOS or Windows

## Installation

```bash
git clone https://github.com/RakaSP/SPARS-handson.git
cd SPARS-handson
python install.py
```

`install.py` creates the virtual environment `SPARS-venv`, installs the PyTorch build that fits the machine (NVIDIA, AMD, Intel or CPU) and installs SPARS. It ends with `Setup complete.` Activate the environment in every new terminal:

```bash
source SPARS-venv/bin/activate        # Linux, macOS
.\SPARS-venv\Scripts\Activate.ps1     # Windows PowerShell
```

`setup_guide.pdf` walks through every step, from installing Python to starting the dashboard.

## Main workflow

A SPARS run uses three inputs:

1. **Workload:** the jobs, with submission time, requested nodes, requested time and actual runtime.
2. **Platform:** the nodes, with their power in each state, transition times and DVFS profiles.
3. **Experiment definition:** which workload, platform, scheduler and power manager to run.

```text
workload JSON + platform JSON -> experiment definition -> dashboard: run -> results/ -> metrics and Gantt plots
```

## 1. Get a workload

Workloads are JSON files that you generate or convert. A workload is one object:

```json
{
  "nb_res": 8,
  "jobs": [{"job_id": 1, "res": 2, "subtime": 10.5, "reqtime": 400.0, "runtime": 280.0, "user_id": 0}]
}
```

`res` is the number of nodes requested, `subtime`, `reqtime` and `runtime` are simulator seconds.

- **Generate:** dashboard key `g`, from a target utilization.
- **Convert a real trace:** SWF traces (CS HUJI Parallel Workloads Archive) in `data/workloads/cshuji/` are converted with the dashboard's Workloads screen (`w`), or `python -m spars.core.workloads.swf SOURCE.swf OUTPUT.json`.
- **Slice:** the Workloads screen cuts a job range; the `-1-500` suffix in a file name means jobs 1 to 500.

## 2. Get a platform

Platforms are JSON files in `data/platforms/`, and you generate them. Each machine has DVFS profiles and four states (`active`, `sleeping`, `switching_on`, `switching_off`) with their power, transitions and transition times. A node runs jobs only while `active`.

- **Generate:** dashboard key `p` (one base DVFS profile by default, more can be added).
- **Size:** a platform needs at least as many nodes as the workload's `nb_res` (the Workloads screen lists it). For the cshuji traces: DAS2-fs0 144, the other DAS2 traces 64, SDSC-BLUE 1152.

## 3. Write an experiment definition

A definition is a Python file in `src/spars/experiments/<Set>/`:

```python
EXPERIMENT_SET_ORDER = 1

EXPERIMENTS = [
    {
        "name": "FCFS",
        "enabled": True,
        "overrides": {
            "paths": {"workload": "workloads/json/<your-workload>.json", "platform": "platforms/<your-platform>.json"},
            "run": {
                "scheduler": {"name": "fcfs_psus", "config": {}},
                "power_management": {"name": "psus_timeout", "config": {"timeout": 1800}},
            },
        },
    },
]
```

`overrides` changes only the listed values of the shared base configuration. A `sweep` (for example `{"run.power_management.config.timeout": [1800, 3600]}`) runs one experiment per value. Edit the file directly, or use the dashboard's forms (`e`). Your definitions go in the set `src/spars/experiments/MySet/`; in the dashboard, `e` creates a definition file there and `s` then `n` makes another set.

### Schedulers and power managers

A **scheduler** decides which job runs on which nodes. A **power manager** decides which nodes are switched on or off.

| | Names |
|---|---|
| Schedulers | `fcfs_psus`, `easy_psus`, `snf_psas`, `snf_icon`, `snf_oracle_psas`, and more in `src/spars/policies/catalog.py` |
| Power managers | `timeout`, `psus_timeout`, `icon`, `oracle`, `none`, `rl` |

- **PSUS:** power-state-unaware scheduling; it wakes sleeping nodes when a job needs them.
- **PSAS:** power-state-aware scheduling, paired with a power manager that switches nodes proactively.
- **Oracle:** reads the whole workload, including jobs that have not arrived. It is an upper bound, not a real policy.

A policy is named by its catalog name, by the class name of any policy under `src/spars/policies/`, or by a `"module:Class"` path.

## 4. Run SPARS

```bash
python -m spars.cli 8
```

The number is how many simulation cores to use: each running simulation gets one core. The dashboard and plotting use the support cores (`--support-cores`, default 1).

In the dashboard: space selects a definition, Enter goes to the options (plots, log level, cores), Enter again starts the batch. Other keys: `e` experiments, `w` workloads, `g` / `p` generate, `l` log, `r` results, `c` cancel a batch, `ctrl+r` restart (after adding a policy file). Without a terminal, the CLI runs every definition once.

## 5. Inspect the results

Each batch writes to `results/<Set>-NNNNNN/`, one folder per run, with the configuration used, per-job and per-node CSV logs and `metrics.csv` (mean waiting time, wasted energy, energy per state). The dashboard's Results screen (`r`) shows mean waiting time and wasted energy of every run.

## 6. Plot the results

Metrics and Gantt plots are made after a batch when they are switched on in the options. To make them again without running anything:

```bash
python -m spars.cli --plot-only results/<Set>-NNNNNN --metrics --gantt
```

## How one simulation step works

The simulator jumps from event to event (a job arrives, a job ends, a node finishes switching). After processing the events of one time, the Planner asks whether a new plan is needed: the general triggers, the scheduler and the power manager can each say yes. If one does, the Planner calls `scheduler.schedule()` and then `power_manager.decide(now, decision)`. Both answer with events (`execution_start`, `switch_on`, ...) that the simulator queues and executes at their time.

`spars-overview.html` is an interactive diagram of the experiment, core and policies, and of one simulation step. Open it in a browser and click a component to see its code.

`spars-flow.html` is the flowchart of one run, from start to result files. Use Continue or Play to follow it step by step; every step names the code behind it.

## Adding a scheduler or power manager

Put a class anywhere under `src/spars/policies/` and name it in a definition; it is found without registration. The simplest start is to extend an existing policy, for example `FCFSPSUS` in `policies/scheduler/fcfs/psus.py`.

- A scheduler takes `(machines, jobs_manager, start_time, config)` and implements `set_time(t)` and `schedule()`.
- A power manager takes `(machines, start_time, config)` and implements `prepare(t)`, `decide(t, decision)` and `remaining_idle_timeout(node_id, t)`. `policies/power_management/timeout/policy.py` is the reference.

Policies read the requested time of a job, never its actual runtime; only the Oracle may.

## Structure

```
src/spars/
  core/              the simulator and its tooling; no need to read it to write a policy
    simulator/       event loop, JobsManager, PlatformControl, Machine, Monitor
    planner.py       when to plan; calls the scheduler, then the power manager
    trigger.py       the general plan triggers
    experiments/     finds, expands and runs the experiment definitions
    runner/          one run: run_with_config, CPU cores, training and evaluation
    gym/             the RL environment
    dashboard/       the terminal dashboard
    analysis/        metrics, Gantt charts, comparison plots
    workloads/       workload generator, SWF converter
    platforms/       platform generator
    output.py        writes the logs and metrics.csv
  policies/          schedulers and power managers, including the RL agents
    scheduler/       fcfs/, easy/, snf/ and their shared base/
    power_management/  timeout/, icon.py, oracle.py, none.py, rl/
    shared/          helpers both kinds use
    catalog.py       the short names a definition may use
  experiments/       your experiment definitions, one folder per set (empty at the start)
data/                SWF traces; platforms and workloads are made in the dashboard
spars-overview.html  architecture and one simulation step
```

## License

SPARS is released under the MIT License. Please credit the project when you use or modify it in your own work.
