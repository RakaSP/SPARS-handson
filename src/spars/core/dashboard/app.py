"""SPARS dashboard: select runs, set options, follow progress, read results.

Flow: Select run -> Options -> Start (queued) -> back to Select run, where the
progress panel follows the running batch. The app stays open until quit.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path

import yaml
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Input, OptionList, RichLog, SelectionList, Static

from spars.core.registry import resolve_run_components
from spars.core.experiments.configuration import merge_overrides
from spars.core.experiments.defaults import BASE_CONFIG, PROJECT_ROOT
from spars.core.experiments.discovery import (
    ExperimentDefinition,
    discover_experiment_definitions,
    discover_research_names,
)
from spars.core.logging import _ALLOWED_LEVELS, TRACE_LEVEL_NUM
from spars.core.runner.cores import available_cpus, physical_cores
from spars.core.simulator.simulator import validate_workload
from spars.core.workloads.swf import convert_swf

from .results import STATE_COLUMNS, list_run_folders, read_metrics
from .generate import GeneratePlatformScreen, GenerateWorkloadScreen
from .runner import Batch, BatchRunner
from .snapshot import Snapshot
from .settings import LOG_LEVELS, DashboardSettings


_ACTIVE = "bold underline reverse"
_INACTIVE = "dim"


def definition_details(definition: ExperimentDefinition, plots: dict[str, bool]) -> str:
    """The effective settings of every experiment in a definition, as YAML."""
    details = []
    for experiment in definition.experiments:
        cfg = merge_overrides(BASE_CONFIG, experiment.get("overrides", {}))
        scheduler, power = resolve_run_components(cfg["run"])
        cfg["run"].update(scheduler=scheduler, power_management=power)
        cfg["run"].pop("algorithm", None)
        cfg["run"].pop("algo_config", None)
        cfg["reporting"] = dict(plots)
        details.append(
            {
                "name": experiment["name"],
                "display_name": experiment.get("display_name", experiment["name"]),
                "enabled": experiment.get("enabled", True),
                "config": cfg,
                "sweep": experiment.get("sweep", {}),
                "test_overrides": experiment.get("test_overrides", {}),
                "curriculum": experiment.get("curriculum", []),
            }
        )
    return yaml.safe_dump(details, sort_keys=False)


class Choice(Widget, can_focus=True):
    """One option row; the active choice is underlined and highlighted."""

    LABEL_WIDTH = 20  # text columns before the choices, including the indent

    DEFAULT_CSS = """
    Choice { height: 1; padding: 0 1 0 3; }
    Choice:focus { background: $primary 40%; }
    """
    BINDINGS = [
        Binding("left", "move(-1)", "Previous", show=False),
        Binding("right", "move(1)", "Next", show=False),
    ]

    class Changed(Message):
        def __init__(self, choice: "Choice") -> None:
            super().__init__()
            self.choice = choice

    def __init__(self, label: str, choices: list[str], value: str, *, id: str) -> None:
        super().__init__(id=id)
        self.label = label.strip()
        self.indent = len(label) - len(label.lstrip())
        self.styles.padding = (0, 1, 0, 3 + self.indent)  # leading spaces indent
        self.choices = choices
        self.index = choices.index(value) if value in choices else 0

    @property
    def value(self) -> str:
        return self.choices[self.index]

    def set_choices(self, choices: list[str]) -> None:
        value = self.value
        self.choices = choices
        self.index = choices.index(value) if value in choices else len(choices) - 1
        self.refresh()

    def action_move(self, step: int) -> None:
        index = min(len(self.choices) - 1, max(0, self.index + step))
        if index != self.index:
            self.index = index
            self.refresh()
            self.post_message(self.Changed(self))

    def render(self) -> Text:
        text = Text()
        text.append(self._label_text())
        for index, choice in enumerate(self.choices):
            text.append(f" {choice} ", style=_ACTIVE if index == self.index else _INACTIVE)
            text.append(" ")
        return text

    def _label_text(self) -> str:
        width = self.LABEL_WIDTH - self.indent
        label = self.label if len(self.label) < width else self.label[: width - 1] + "…"
        return f"{label:<{width}}"

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()


class NumberChoice(Choice):
    """A number with bounds, shown as ‹ n ›."""

    def render(self) -> Text:
        text = Text()
        text.append(self._label_text())
        text.append("‹ ", style=_INACTIVE if self.index == 0 else "")
        text.append(f" {self.value} ", style=_ACTIVE)
        text.append(" ›", style=_INACTIVE if self.index == len(self.choices) - 1 else "")
        text.append(f"   (max {self.choices[-1]})", style=_INACTIVE)
        return text


KEYS_HELP = """\
Main screen
  Space      select / unselect an experiment definition      a  select all
  Enter      next: choose options, then start the run         d  show the settings of a definition
  e          create, edit and delete experiments              w  workloads: import a CS HUJI .swf log
  g          generate a synthetic workload                    p  generate a platform
  r          results of finished runs                         l  log of the running batch
  s          choose another set, or create a new one     c  cancel a queued or running batch
  q          quit                                             ?  this help

Everywhere
  Esc        go back (a form asks before dropping unsaved changes)
  Up / Down  move between fields        Left / Right  change a Yes/No or option field
  Tab        accept the highlighted suggestion in a list, otherwise next field
  Mouse      click a field to edit it, click a suggestion or a button

Experiment form
  Ctrl+S     save                        Esc  cancel
  The line above the buttons explains the field you are on.

Experiment lists
  n  new      Enter  edit/open      u  duplicate      x  delete
"""


class SelectRunScreen(Screen):
    BINDINGS = [
        Binding("enter", "options", "Next", priority=True),
        Binding("a", "toggle_all", "All"),
        Binding("d", "details", "Details"),
        Binding("s", "research", "Set"),
        Binding("r", "results", "Results"),
        Binding("w", "workloads", "Workloads"),
        Binding("g", "generate_workload", "New workload"),
        Binding("p", "generate_platform", "New platform"),
        Binding("e", "experiments", "Experiments"),
        Binding("l", "log", "Log"),
        Binding("c", "cancel", "Cancel run"),
        Binding("ctrl+r", "app.restart", "Restart"),
        Binding("question_mark", "keys", "Help"),
        Binding("q", "app.request_quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        yield Static(id="code-warning")
        yield Static("Select run   (space toggles)", classes="section")
        yield SelectionList(id="definitions")
        yield Static(id="definitions-empty", classes="hint")
        yield Static("Progress", classes="section")
        yield Static(id="summary")
        yield DataTable(id="workers", show_cursor=False)
        yield Static(id="queue")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#workers", DataTable)
        for column in ("Worker", "Simulation", "Phase", "Epoch", "Progress", "Jobs", "Elapsed"):
            table.add_column(column)
        self.load_definitions()
        self.set_interval(0.5, self.refresh_progress)
        self.refresh_progress()

    @property
    def dashboard(self) -> "SparsDashboard":
        return self.app  # type: ignore[return-value]

    def show_code_warning(self, changed: list[str]) -> None:
        warning = self.query_one("#code-warning", Static)
        if not changed:
            warning.update("")
            return
        shown = ", ".join(changed[:3]) + (f" and {len(changed) - 3} more" if len(changed) > 3 else "")
        warning.update(
            Text(
                f"Code changed: {shown}. Runs still use the old code; press ctrl+r to restart and apply.",
                style="bold yellow",
            )
        )

    def load_definitions(self) -> None:
        app = self.dashboard
        selection = self.query_one("#definitions", SelectionList)
        selection.clear_options()
        remembered = set(app.settings.selected)
        keys = [definition.key for definition in app.definitions]
        # First visit (or nothing remembered for this research): select all.
        chosen = remembered & set(keys) or set(keys)
        for definition in app.definitions:
            selection.add_option(
                (definition.label, definition.key, definition.key in chosen)
            )
        selection.focus()
        self.query_one("#definitions-empty", Static).update(
            "" if app.definitions
            else f"No experiments in set {app.research}. Press e to create one."
        )
        self.refresh_title()

    def refresh_title(self) -> None:
        settings = self.dashboard.settings
        self.query_one("#title", Static).update(
            Text.assemble(
                ("SPARS", "bold"),
                f"  ·  set {self.dashboard.research}",
                f"  ·  {settings.simulation_cores} Simulation + "
                f"{settings.support_cores} Support cores",
            )
        )

    def selected_keys(self) -> list[str]:
        return list(self.query_one("#definitions", SelectionList).selected)

    def refresh_progress(self) -> None:
        runner = self.dashboard.runner
        batch = runner.current
        table = self.query_one("#workers", DataTable)
        table.clear()
        if batch is None:
            self.query_one("#summary", Static).update(Text("Idle", style=_INACTIVE))
        else:
            state = batch.state
            failed = f" · {state['failed']} failed" if state.get("failed") else ""
            self.query_one("#summary", Static).update(
                f"{batch.name}  ·  {state.get('completed', 0)}/{state.get('total', '?')} "
                f"simulations{failed}  ·  {state.get('activity', 'Starting')}  ·  "
                f"{state.get('elapsed', '0s')}"
            )
            for worker in state.get("workers", []):
                epoch = f"{worker['epoch']}/{worker['epochs']}" if worker["epochs"] else "-"
                phase = worker["phase"]
                if worker["status"] in {"loading", "saving", "finalizing", "done", "failed"}:
                    phase = worker["status"]
                table.add_row(
                    f"W{worker['slot']}",
                    worker["label"] or "-",
                    phase.capitalize(),
                    epoch,
                    worker["progress"],
                    worker["jobs"],
                    worker["elapsed"],
                )
        lines = [
            f"{b.name}: {b.status}"
            for b in runner.batches
            if b.status != "running"
        ][-5:]
        waiting = len(runner.waiting)
        header = f"Queue: {waiting} waiting" if waiting else "Queue: empty"
        self.query_one("#queue", Static).update("\n".join([header, *lines]))

    def action_toggle_all(self) -> None:
        selection = self.query_one("#definitions", SelectionList)
        if len(selection.selected) == selection.option_count:
            selection.deselect_all()
        else:
            selection.select_all()

    def action_options(self) -> None:
        keys = self.selected_keys()
        if not keys:
            self.notify("Select at least one experiment.", severity="warning")
            return
        self.dashboard.push_screen(OptionsScreen(keys))

    def action_details(self) -> None:
        selection = self.query_one("#definitions", SelectionList)
        if selection.highlighted is None:
            return
        definition = self.dashboard.definitions[selection.highlighted]
        settings = self.dashboard.settings
        self.dashboard.push_screen(
            TextScreen(
                f"Definition: {definition.key}",
                definition_details(
                    definition, {"metrics": settings.metrics, "gantt": settings.gantt}
                ),
            )
        )

    def action_research(self) -> None:
        self.dashboard.push_screen(
            ResearchScreen(discover_research_names()), self.dashboard.set_research
        )

    def action_results(self) -> None:
        self.dashboard.push_screen(ResultsListScreen())

    def action_keys(self) -> None:
        self.dashboard.push_screen(TextScreen("Keys", KEYS_HELP))

    def action_experiments(self) -> None:
        from .experiment_form import DefinitionsScreen

        self.dashboard.push_screen(DefinitionsScreen())

    def action_workloads(self) -> None:
        self.dashboard.push_screen(WorkloadsScreen())

    def action_generate_workload(self) -> None:
        self.dashboard.push_screen(GenerateWorkloadScreen())

    def action_generate_platform(self) -> None:
        self.dashboard.push_screen(GeneratePlatformScreen())

    def action_log(self) -> None:
        self.dashboard.push_screen(LogScreen())

    def action_cancel(self) -> None:
        if not self.dashboard.runner.busy:
            self.notify("No run is active.")
            return
        self.dashboard.push_screen(BatchesScreen())


class BatchesScreen(Screen):
    """Every queued and running batch; Enter cancels the selected one."""

    BINDINGS = [
        Binding("x", "cancel", "Cancel batch"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("Cancel run   ·   Enter cancels the selected batch", id="title")
        yield DataTable(id="batches", cursor_type="row", zebra_stripes=True)
        yield Footer()

    @property
    def runner(self) -> "BatchRunner":
        return self.app.runner  # type: ignore[attr-defined]

    def on_mount(self) -> None:
        table = self.query_one("#batches", DataTable)
        table.add_columns("Batch", "Set", "Definitions", "Status")
        self.refresh_rows()
        self.set_interval(1.0, self.refresh_rows)
        table.focus()

    def active(self) -> list:
        return [b for b in self.runner.batches if b.status in ("queued", "running")]

    def refresh_rows(self) -> None:
        table = self.query_one("#batches", DataTable)
        row = table.cursor_row
        table.clear()
        for batch in self.active():
            table.add_row(batch.name, batch.research, ", ".join(batch.definitions), batch.status, key=str(batch.number))
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        table = self.query_one("#batches", DataTable)
        batches = self.active()
        if not batches:
            return
        batch = batches[table.cursor_row]

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.runner.cancel(batch)
                self.refresh_rows()

        self.app.push_screen(ConfirmScreen(f"Cancel {batch.name} ({batch.status})?"), confirmed)


class OptionsScreen(Screen):
    BINDINGS = [
        Binding("enter", "start", "Start", priority=True),
        Binding("up", "app.focus_previous", "Up", show=False),
        Binding("down", "app.focus_next", "Down", show=False),
        Binding("x", "reset", "Reset to defaults"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        self.keys = keys

    def compose(self) -> ComposeResult:
        settings = self.app.settings  # type: ignore[attr-defined]
        yield Static(f"Options   ·   {len(self.keys)} definitions selected", id="title")
        yield Static("←/→ change   ↑/↓ move   Enter start", classes="section")
        with Vertical(id="options"):
            yield Choice("Metrics plots", ["Yes", "No"], "Yes" if settings.metrics else "No", id="metrics")
            yield Choice("Gantt plots", ["Yes", "No"], "Yes" if settings.gantt else "No", id="gantt")
            yield Choice("Log level", list(LOG_LEVELS), settings.log_level, id="log_level")
            with Horizontal(id="log-file-row"):
                yield Static("  Log file            ")
                yield Input(settings.log_file, placeholder="no log file", id="log_file")
            cores = self._range(1, len(self.app.physical_cores) - 1)  # type: ignore[attr-defined]
            yield NumberChoice("Simulation cores", cores, str(settings.simulation_cores), id="simulation_cores")
            yield NumberChoice("Support cores", cores, str(settings.support_cores), id="support_cores")
        yield Static(id="warning")
        yield Footer()

    @staticmethod
    def _range(low: int, high: int) -> list[str]:
        return [str(value) for value in range(low, max(low, high) + 1)]

    def on_mount(self) -> None:
        self._update_limits()
        self.query_one("#metrics", Choice).focus()

    def _update_limits(self) -> None:
        # Simulation + Support never exceed the physical cores.
        cores = len(self.app.physical_cores)  # type: ignore[attr-defined]
        simulation = self.query_one("#simulation_cores", NumberChoice)
        support = self.query_one("#support_cores", NumberChoice)
        simulation.set_choices(self._range(1, cores - int(support.value)))
        support.set_choices(self._range(1, cores - int(simulation.value)))
        warning = ""
        level = self.query_one("#log_level", Choice).value
        if TRACE_LEVEL_NUM in _ALLOWED_LEVELS[level]:
            warning = f"{level} includes TRACE lines; RL runs can write very large log files."
        self.query_one("#warning", Static).update(Text(warning, style="yellow"))

    def on_choice_changed(self, message: Choice.Changed) -> None:
        self._update_limits()

    def _apply(self) -> DashboardSettings:
        settings: DashboardSettings = self.app.settings  # type: ignore[attr-defined]
        settings.metrics = self.query_one("#metrics", Choice).value == "Yes"
        settings.gantt = self.query_one("#gantt", Choice).value == "Yes"
        settings.log_level = self.query_one("#log_level", Choice).value
        settings.log_file = self.query_one("#log_file", Input).value.strip()
        settings.simulation_cores = int(self.query_one("#simulation_cores", NumberChoice).value)
        settings.support_cores = int(self.query_one("#support_cores", NumberChoice).value)
        settings.selected = self.keys
        return settings

    def action_reset(self) -> None:
        settings: DashboardSettings = self.app.settings  # type: ignore[attr-defined]
        settings.reset()
        self.app.pop_screen()
        self.app.push_screen(OptionsScreen(self.keys))

    def on_input_submitted(self, message: Input.Submitted) -> None:
        self.action_start()

    def action_start(self) -> None:
        app: SparsDashboard = self.app  # type: ignore[assignment]
        settings = self._apply()
        settings.research = app.research
        settings.save()
        ahead = app.runner.busy
        batch = app.runner.submit(
            research=app.research,
            definitions=list(self.keys),
            metrics=settings.metrics,
            gantt=settings.gantt,
            log_level=settings.log_level,
            log_file=settings.log_file,
            simulation_cores=settings.simulation_cores,
            support_cores=settings.support_cores,
        )
        app.notify(f"Batch {batch.number} {'queued' if ahead else 'started'}.")
        app.pop_screen()
        app.main.refresh_title()


class TextScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.pop_screen", "Back", show=False)]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, id="title")
        with VerticalScroll():
            yield Static(self.body, markup=False)
        yield Footer()


class LogScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.pop_screen", "Back", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("Log", id="title")
        yield RichLog(id="log", max_lines=5000, wrap=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        for label, line in self.app.log_lines:  # type: ignore[attr-defined]
            log.write(f"[{label}] {line}")

    def append(self, label: str, line: str) -> None:
        self.query_one("#log", RichLog).write(f"[{label}] {line}")


class ResearchScreen(ModalScreen[str]):
    BINDINGS = [
        Binding("n", "new", "New set"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Select set   (n: new set)", classes="section")
            yield OptionList(*self.names)

    def action_new(self) -> None:
        from spars.core.experiments import store

        from .experiment_form import NameScreen

        def named(name: str | None) -> None:
            if not name:
                return
            try:
                created = store.create_research(name)
            except (OSError, ValueError) as error:
                self.notify(str(error), severity="error")
                return
            self.dismiss(created)
            self.app.notify(f"Set {created} created. Press e to add experiments.")

        self.app.push_screen(NameScreen("New set name (letters, digits, _)"), named)

    def on_option_list_option_selected(self, message: OptionList.OptionSelected) -> None:
        self.dismiss(self.names[message.option_index])


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static(Text.assemble((" y ", _ACTIVE), " Yes    ", (" n ", _INACTIVE), " No"))

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class ResultsListScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.pop_screen", "Back", show=False)]

    def compose(self) -> ComposeResult:
        yield Static("Results   ·   Enter opens a run", id="title")
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        table.add_columns("Run", "Modified", "Metrics files")
        self.folders = list_run_folders()
        for folder in self.folders:
            table.add_row(
                folder.path.name,
                folder.modified.strftime("%Y-%m-%d %H:%M"),
                Text(str(folder.metrics_files), justify="right"),
            )
        table.focus()

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        self.app.push_screen(ResultsTableScreen(self.folders[message.cursor_row].path))


class ResultsTableScreen(Screen):
    BINDINGS = [
        Binding("p", "plot", "Make plots"),
        Binding("escape", "app.pop_screen", "Back"),
        Binding("q", "app.pop_screen", "Back", show=False),
    ]

    def __init__(self, run_folder: Path) -> None:
        super().__init__()
        self.run_folder = run_folder

    def compose(self) -> ComposeResult:
        yield Static(
            f"{self.run_folder.name}   ·   every metrics.csv in this run   ·   columns ending (h) are node-hours: time in each node state, summed over all nodes",
            id="title",
        )
        yield DataTable(id="metrics", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#metrics", DataTable)
        table.add_columns(
            "Experiment", "Workload", "Variant", "Mean wait (s)", "Unfinished", "Status", "Waste (GJ)",
            *(f"{name} (h)" for name, _ in STATE_COLUMNS),
        )
        for row in read_metrics(self.run_folder):
            table.add_row(
                row.experiment,
                row.workload,
                row.variant,
                Text("-" if row.mean_wait is None else f"{row.mean_wait:,.1f}", justify="right"),
                Text("-" if row.unfinished_jobs is None else str(row.unfinished_jobs), justify="right"),
                Text("STALLED" if row.stalled else "ok", style="bold red" if row.stalled else ""),
                Text("-" if row.waste_gj is None else f"{row.waste_gj:,.2f}", justify="right"),
                *(
                    Text("-" if seconds is None else f"{seconds / 3600:,.1f}", justify="right")
                    for seconds in row.state_seconds
                ),
            )
        table.focus()

    def action_plot(self) -> None:
        self.app.notify("Making metrics and Gantt plots ...")
        self.run_worker(self._plot, thread=True, exclusive=True)

    def _plot(self) -> None:
        # Same command as `spars.cli --plot-only`, in its own process.
        done = subprocess.run(
            [sys.executable, "-m", "spars.cli", "--plot-only", str(self.run_folder), "--metrics", "--gantt"],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
            capture_output=True,
            text=True,
        )
        if done.returncode == 0:
            self.app.call_from_thread(self.app.notify, f"Plots saved under {self.run_folder.name}/plots")
        else:
            error = (done.stderr.strip().splitlines() or ["unknown error"])[-1]
            self.app.call_from_thread(self.app.notify, f"Plotting failed: {error}", severity="error")


WORKLOAD_DIR = PROJECT_ROOT / "data" / "workloads" / "json"


class WorkloadsScreen(Screen):
    """List converted workloads and import a CS HUJI SWF log as a new one."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("Workloads   ·   import a CS HUJI .swf / .swf.gz log", id="title")
        yield DataTable(id="workload-list", show_cursor=False)
        yield Static("SWF file (Enter imports)", classes="section")
        yield Input(placeholder="data/workloads/cshuji/DAS2-fs1-2003-1.swf", id="swf_path")
        yield Static("Max jobs (blank = all)", classes="section")
        yield Input(placeholder="3000", id="swf_limit")
        yield Static(id="import-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#workload-list", DataTable)
        table.add_columns("File", "Jobs", "nb_res")
        self.refresh_list()
        self.query_one("#swf_path", Input).focus()

    def refresh_list(self) -> None:
        table = self.query_one("#workload-list", DataTable)
        table.clear()
        for path in sorted(WORKLOAD_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                row = (str(len(data["jobs"])), str(data["nb_res"]))
            except (OSError, ValueError, KeyError, TypeError):
                row = ("invalid", "-")
            table.add_row(path.name, *row)

    def status(self, message: str, error: bool = False) -> None:
        self.query_one("#import-status", Static).update(
            Text(message, style="red" if error else "green")
        )

    def on_input_submitted(self, message: Input.Submitted) -> None:
        source = Path(self.query_one("#swf_path", Input).value.strip()).expanduser()
        limit_text = self.query_one("#swf_limit", Input).value.strip()
        if not source.is_absolute():
            source = PROJECT_ROOT / source
        if limit_text and (not limit_text.isdigit() or int(limit_text) < 1):
            self.status("Max jobs must be a positive integer.", error=True)
            return
        limit = int(limit_text) if limit_text else None
        self.status("Converting...")
        self.run_worker(lambda: self.convert(source, limit), thread=True)

    def convert(self, source: Path, limit: int | None) -> None:
        # Runs in a thread: big logs have hundreds of thousands of jobs.
        try:
            stem = re.sub(r"\.swf(\.gz)?$", "", source.name)
            target = WORKLOAD_DIR / f"{stem}-1-{limit}.json" if limit else WORKLOAD_DIR / f"{stem}.json"
            if target.exists():
                raise FileExistsError(f"{target.name} already exists; it is not overwritten.")
            workload = convert_swf(source, limit)
            validate_workload(workload, workload["nb_res"])
            target.parent.mkdir(parents=True, exist_ok=True)  # data/workloads/json is not shipped in the hands-on repository
            temporary = target.with_name(f".{target.name}.tmp")  # a crash leaves no partial JSON
            temporary.write_text(json.dumps(workload, indent=4) + "\n", encoding="utf-8")
            os.replace(temporary, target)
            message = (
                f"Wrote {len(workload['jobs'])} jobs to workloads/json/{target.name}  "
                "(use this path as the experiment workload)"
            )
            self.app.call_from_thread(self.finish, message, False)
        except Exception as error:  # shown to the user, not swallowed
            self.app.call_from_thread(self.finish, f"{type(error).__name__}: {error}", True)

    def finish(self, message: str, error: bool) -> None:
        self.status(message, error)
        self.refresh_list()


class SparsDashboard(App):
    TITLE = "SPARS"
    CSS = """
    Screen { padding: 0 1; }
    #title { padding: 1 0 0 0; text-style: bold; }
    .section { padding: 1 0 0 0; color: $text-muted; }
    #definitions { height: auto; max-height: 45%; border: round $primary; }
    #workers { height: auto; max-height: 12; }
    #summary { padding: 0 0 0 1; }
    #queue { padding: 1 0 0 1; color: $text-muted; }
    #options { height: auto; border: round $primary; padding: 1 0; }
    #log-file-row { height: 1; }
    #log-file-row Static { width: 22; padding: 0 0 0 1; }
    #log_file { width: 48; }
    #warning { padding: 1 0 0 1; }
    #code-warning { padding: 0 0 0 1; }
    #log { border: round $primary; }
    #form > .row, #form .dvfs-row { height: 1; }
    .dvfs-row Input.dvfs-name { width: 24; }
    .dvfs-row Input.dvfs-factor { width: 10; margin: 0 0 0 1; }
    .dvfs-row .dvfs-remove { margin: 0 0 0 1; min-width: 10; }
    #dvfs-rows { height: auto; }
    #preview { padding: 1 0 0 1; }
    #status { padding: 0 1; }
    .dialog { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    ResearchScreen, ConfirmScreen, NameScreen, YamlScreen { align: center middle; }
    .wide { width: 90; }
    #yaml { height: 20; }
    #yaml-error { color: $error; }
    #form { height: 1fr; }
    #errors { height: auto; max-height: 6; padding: 0 1; }
    .row { height: 1; }
    .row-label { width: 34; text-wrap: nowrap; text-overflow: ellipsis; }
    .row Input { width: 40; }
    .row Input.kind-int, .row Input.kind-float, .row Input.kind-num, .row Input.kind-numstr { width: 16; }
    /* Narrow terminals (under 105 columns): shorter label column, boxes fill the rest. */
    .narrow .row-label, .narrow Picker .picker-label { width: 24; }
    .narrow .row Input, .narrow Picker Input { width: 1fr; }
    .narrow .row Input.kind-int, .narrow .row Input.kind-float, .narrow .row Input.kind-num, .narrow .row Input.kind-numstr { width: 16; }
    .narrow Picker OptionList { margin: 0 0 0 24; width: 1fr; }
    .narrow .sweep-values { width: 20; }
    .subsection { padding: 1 0 0 3; color: $text-muted; text-style: bold; }
    .group { height: auto; }
    .hint { color: $text-muted; padding: 0 0 0 3; }
    .group Button { margin: 0 0 0 3; }
    .buttons { height: 1; margin: 1 0 0 0; padding: 0 1; }
    .buttons Button { margin: 0 2 0 0; }
    .sweep-row { height: auto; }
    .sweep-row Picker { width: 1fr; }
    .sweep-values { width: 30; }
    .sweep-row .sweep-remove { margin: 0 0 0 1; min-width: 10; }
    .sweep-row Input.sweep-values { margin: 0 0 0 1; }
    #help { height: auto; min-height: 1; margin: 1 0 0 0; padding: 0 1; }
    #form { scrollbar-size: 1 1; }
    Input { height: 1; border: none; padding: 0 1; background: $boost; }
    Input:focus { border: none; background: $primary 40%; }
    Button { height: 1; min-width: 14; border: none; }
    Button:focus { text-style: bold reverse; }
    DataTable { scrollbar-size: 1 1; }
    /* Only selected definitions show their mark. */
    SelectionList > .selection-list--button { color: $surface; background: $surface; }
    SelectionList > .selection-list--button-highlighted { color: $surface; background: $surface; }
    SelectionList > .selection-list--button-selected { color: $success; text-style: bold; }
    SelectionList > .selection-list--button-selected-highlighted { color: $success; text-style: bold; }
    """
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "request_quit", "Quit", show=False, priority=True)]

    def __init__(self, settings: DashboardSettings, available_cpus: set[int]) -> None:
        super().__init__()
        self.settings = settings
        self.physical_cores = physical_cores(available_cpus)
        self.log_lines: deque[tuple[str, str]] = deque(maxlen=5000)
        self.snapshot = Snapshot()
        self.restart_requested = False
        self.runner = BatchRunner(
            available_cpus,
            self.on_log,
            self.on_batch_finished,
            self.on_run_failed,
            python_path=self.snapshot.python_path,
        )
        names = discover_research_names()
        self.research = settings.research if settings.research in names else (names[0] if names else "")
        self.startup_error = "" if names else (
            "No experiment folder yet: create src/spars/experiments/<Set>/ and write a definition in it, then ctrl+r."
        )
        self.definitions = []
        try:
            if names:
                self.definitions = discover_experiment_definitions(self.research)
        except Exception as error:  # a hand-broken file must not stop the dashboard
            self.startup_error = f"Cannot read definitions: {error}"
        self.main = SelectRunScreen()

    def on_mount(self) -> None:
        self.push_screen(self.main)
        if self.startup_error:
            self.notify(self.startup_error, severity="error", timeout=30)
        self.set_interval(3, self.check_code_changes)

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 105, "narrow")

    def on_unmount(self) -> None:
        self.snapshot.remove()

    def check_code_changes(self) -> None:
        changed = self.snapshot.changed_files()
        self.main.show_code_warning(changed)

    async def action_restart(self) -> None:
        """Stop active runs, then start the dashboard again on the current code."""

        async def restart() -> None:
            await self.runner.shutdown()
            self.restart_requested = True
            self.exit()

        if not self.runner.busy:
            await restart()
            return

        async def confirmed(yes: bool | None) -> None:
            if yes:
                await restart()

        self.push_screen(ConfirmScreen("Runs are active. Stop them and restart?"), confirmed)

    def reload_definitions(self) -> None:
        """Re-read the definition files (after the dashboard edited one)."""
        # Only the definition modules (spars.experiments.<research>.<file>); the
        # packages and any helper module a definition imports stay loaded.
        for name in [
            n for n in sys.modules
            if n.startswith("spars.experiments.") and n.count(".") == 3 and not n.rsplit(".", 1)[1].startswith("_")
        ]:
            del sys.modules[name]
        try:
            self.definitions = discover_experiment_definitions(self.research)
        except Exception as error:  # a hand-broken file must not close the dashboard
            self.notify(f"Cannot read definitions: {error}", severity="error")
            return
        self.main.load_definitions()

    def set_research(self, research: str | None) -> None:
        if research is None or research == self.research:
            return
        self.research = research
        self.definitions = discover_experiment_definitions(research)
        self.main.load_definitions()

    def on_log(self, label: str, line: str) -> None:
        self.log_lines.append((label, line))
        if isinstance(self.screen, LogScreen):
            self.screen.append(label, line)

    def on_run_failed(self, batch: Batch, label: str) -> None:
        """Alert at once; the other runs of the batch keep going."""
        self.notify(
            f"Error in batch run {batch.name}: {label} failed. Press l for details.",
            title="SPARS",
            severity="error",
            timeout=30,
        )
        self.bell()

    def on_batch_finished(self, batch: Batch) -> None:
        if batch.status == "done":
            self.notify(f"Run {batch.name} finished.", title="SPARS")
        elif batch.status == "cancelled":
            self.notify(f"Run {batch.name} cancelled.", title="SPARS", severity="warning")
        else:
            self.notify(
                f"Error in batch run {batch.name}"
                + (f": {len(batch.failed_runs)} run(s) failed" if batch.failed_runs else "")
                + ". Press l for details.",
                title="SPARS",
                severity="error",
                timeout=30,
            )
        self.bell()

    async def action_request_quit(self) -> None:
        if not self.runner.busy:
            self.exit()
            return

        async def confirmed(yes: bool | None) -> None:
            if yes:
                await self.runner.shutdown()
                self.exit()

        self.push_screen(ConfirmScreen("Runs are active. Stop them and quit?"), confirmed)


def run_dashboard(settings: DashboardSettings) -> None:
    app = SparsDashboard(settings, available_cpus())
    try:
        app.run()
    finally:
        app.snapshot.remove()
    if app.restart_requested:
        # The same command line, on the files as they are now.
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
