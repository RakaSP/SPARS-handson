"""Screens that generate a synthetic workload or a platform file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

from spars.core.experiments.defaults import PROJECT_ROOT
from spars.core.platforms.generate import (
    DVFSProfile,
    PlatformConfig,
    generate_platform,
    write_platform,
)
from spars.core.simulator.simulator import validate_workload
from spars.core.workloads.generate import (
    GeneratorConfig,
    generate_workload,
    mean_interarrival,
    summarize,
    write_workload,
)

WORKLOAD_DIR = PROJECT_ROOT / "data" / "workloads" / "json"
PLATFORM_DIR = PROJECT_ROOT / "data" / "platforms"
NAME = re.compile(r"[A-Za-z0-9_.-]+")


class FormError(ValueError):
    """A field the user must correct; the message names it."""


def _number(text: str, label: str, kind: type = float) -> Any:
    try:
        return kind(text.strip())
    except ValueError:
        raise FormError(f"{label} must be {'an integer' if kind is int else 'a number'}.") from None


def _file_name(text: str) -> str:
    name = text.strip().removesuffix(".json")
    if not NAME.fullmatch(name):
        raise FormError("File name: use letters, digits, '_', '-' and '.' only.")
    return name


class GenerateScreen(Screen):
    """Rows of label + Input, a help line for the focused field, Ctrl+S to generate."""

    BINDINGS = [
        Binding("ctrl+s", "generate", "Generate", priority=True),
        Binding("escape", "app.pop_screen", "Back"),
    ]
    TITLE_TEXT = ""
    HELP: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static(self.TITLE_TEXT, id="title")
        with VerticalScroll(id="form"):
            yield from self.rows()
        yield Static(id="help")
        yield Static(id="status")
        yield Footer()

    def rows(self) -> ComposeResult:
        raise NotImplementedError

    def row(self, key: str, label: str, value: Any) -> Horizontal:
        return Horizontal(
            Static(label, classes="row-label"), Input(str(value), id=key), classes="row"
        )

    def value(self, key: str) -> str:
        return self.query_one(f"#{key}", Input).value

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        key = event.widget.id or ""
        self.query_one("#help", Static).update(self.HELP.get(key, ""))

    def status(self, message: str, error: bool = False) -> None:
        self.query_one("#status", Static).update(Text(message, style="red" if error else "green"))

    def action_generate(self) -> None:
        try:
            self.status(self.generate())
        except (FormError, ValueError, FileExistsError, OSError) as error:
            self.status(str(error), error=True)

    def generate(self) -> str:
        raise NotImplementedError


class GenerateWorkloadScreen(GenerateScreen):
    TITLE_TEXT = "Generate workload   ·   Ctrl+S writes data/workloads/json/<file name>.json"
    HELP = {
        "name": "File name without .json. An existing file is never overwritten.",
        "num_jobs": "Number of jobs.",
        "nb_res": "Nodes of the platform the workload is made for; no job asks for more.",
        "target_utilization": "Share of the platform's node-time the jobs ask for, in (0, 1]. It sets the arrival rate.",
        "mean_runtime": "Mean actual runtime of a job in seconds (exponential).",
        "resource_geometric_p": "Nodes per job are geometric with this p, cut at nb_res. A larger p gives smaller jobs.",
        "reqtime_bias": "Mean requested time = mean runtime x this (exponential, drawn independently).",
        "seed": "Random seed: the same settings and seed give the same workload.",
        "profile_name": "Key every job uses in the workload's profiles.",
    }

    def rows(self) -> ComposeResult:
        d = GeneratorConfig()
        yield self.row("name", "File name", "synthetic-3000")
        yield self.row("num_jobs", "Jobs", d.num_jobs)
        yield self.row("nb_res", "Platform nodes (nb_res)", d.nb_res)
        yield self.row("target_utilization", "Target utilization", d.target_utilization)
        yield self.row("mean_runtime", "Mean runtime (s)", d.mean_runtime)
        yield self.row("resource_geometric_p", "Resource geometric p", d.resource_geometric_p)
        yield self.row("reqtime_bias", "Requested time bias", d.reqtime_bias)
        yield self.row("seed", "Seed", d.seed)
        yield self.row("profile_name", "Profile name", d.profile_name)
        yield Static(id="preview")

    def config(self) -> GeneratorConfig:
        return GeneratorConfig(
            num_jobs=_number(self.value("num_jobs"), "Jobs", int),
            nb_res=_number(self.value("nb_res"), "Platform nodes", int),
            target_utilization=_number(self.value("target_utilization"), "Target utilization"),
            mean_runtime=_number(self.value("mean_runtime"), "Mean runtime"),
            resource_geometric_p=_number(self.value("resource_geometric_p"), "Resource geometric p"),
            reqtime_bias=_number(self.value("reqtime_bias"), "Requested time bias"),
            seed=_number(self.value("seed"), "Seed", int),
            profile_name=self.value("profile_name").strip(),
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        try:
            text = f"Mean interarrival time: {mean_interarrival(self.config()):.0f} s"
        except (FormError, ValueError, ZeroDivisionError):
            text = ""
        self.query_one("#preview", Static).update(Text(text, style="dim"))

    def generate(self) -> str:
        name = _file_name(self.value("name"))
        config = self.config()
        workload = generate_workload(config)
        validate_workload(workload, config.nb_res)
        write_workload(workload, WORKLOAD_DIR / f"{name}.json")
        return f"Wrote workloads/json/{name}.json: {summarize(config, workload)}"


class GeneratePlatformScreen(GenerateScreen):
    TITLE_TEXT = "Generate platform   ·   Ctrl+S writes data/platforms/<file name>.json"
    HELP = {
        "name": "File name without .json. An existing file is never overwritten.",
        "num_nodes": "Number of identical nodes.",
        "node_name": "Name of the node type.",
        "base_compute_power": "Power in W while computing, at the base DVFS profile.",
        "base_idle_power": "Power in W while idle, at the base DVFS profile.",
        "switching_on_power": "Power in W while a node switches on.",
        "switching_off_power": "Power in W while a node switches off.",
        "sleeping_power": "Power in W while a node sleeps.",
        "switching_on_time": "Seconds to switch a node on.",
        "switching_on_std": "Standard deviation of that time in seconds (0 = always the same).",
        "switching_off_time": "Seconds to switch a node off.",
        "switching_off_std": "Standard deviation of that time in seconds.",
        "compute_speed": "Relative compute speed of the nodes at the base profile.",
        "dvfs_mode": "The DVFS profile every node starts in; it must be one of the profiles below.",
        "add-dvfs": "Add a DVFS profile: factors that scale the base compute power, idle power and compute speed.",
    }

    def rows(self) -> ComposeResult:
        d = PlatformConfig()
        yield self.row("name", "File name", "my-platform")
        yield self.row("num_nodes", "Nodes", d.num_nodes)
        yield self.row("node_name", "Node name", d.node_name)
        yield self.row("base_compute_power", "Compute power (W)", d.base_compute_power)
        yield self.row("base_idle_power", "Idle power (W)", d.base_idle_power)
        yield self.row("switching_on_power", "Switching-on power (W)", d.switching_on_power)
        yield self.row("switching_off_power", "Switching-off power (W)", d.switching_off_power)
        yield self.row("sleeping_power", "Sleeping power (W)", d.sleeping_power)
        yield self.row("switching_on_time", "Switching-on time (s)", d.switching_on_time)
        yield self.row("switching_on_std", "Switching-on std (s)", d.switching_on_std)
        yield self.row("switching_off_time", "Switching-off time (s)", d.switching_off_time)
        yield self.row("switching_off_std", "Switching-off std (s)", d.switching_off_std)
        yield self.row("compute_speed", "Compute speed", d.compute_speed_variation)
        yield Static("DVFS profiles   (name, then factors of compute power, idle power, compute speed)", classes="section")
        yield Vertical(id="dvfs-rows", classes="group")
        yield self.row("dvfs_mode", "Initial DVFS mode", d.dvfs_mode)
        yield Horizontal(Button("Add DVFS profile", id="add-dvfs"), classes="buttons")

    async def on_mount(self) -> None:
        self._counter = 0
        await self.query_one("#dvfs-rows").mount(self.dvfs_row(DVFSProfile("base")))
        self.query_one(Input).focus()

    def dvfs_row(self, profile: DVFSProfile | None = None) -> Horizontal:
        self._counter += 1
        n = self._counter
        profile = profile or DVFSProfile("")
        return Horizontal(
            Input(profile.name, placeholder="name", classes="dvfs-name", id=f"dvfs-name-{n}"),
            Input(str(profile.compute_power), classes="dvfs-factor", id=f"dvfs-compute-{n}"),
            Input(str(profile.idle_power), classes="dvfs-factor", id=f"dvfs-idle-{n}"),
            Input(str(profile.compute_speed), classes="dvfs-factor", id=f"dvfs-speed-{n}"),
            Button("Remove", id=f"dvfs-remove-{n}", classes="dvfs-remove"),
            classes="dvfs-row",
        )

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        key = event.widget.id or ""
        if key.startswith("dvfs-name"):
            text = "Name of this DVFS profile."
        elif key.startswith("dvfs-compute"):
            text = "Factor on the base compute power (1 = unchanged)."
        elif key.startswith("dvfs-idle"):
            text = "Factor on the base idle power."
        elif key.startswith("dvfs-speed"):
            text = "Factor on the compute speed; a higher speed finishes jobs sooner."
        elif key.startswith("dvfs-remove"):
            text = "Remove this DVFS profile (one must remain)."
        else:
            text = self.HELP.get(key, "")
        self.query_one("#help", Static).update(text)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        rows = self.query_one("#dvfs-rows")
        if event.button.id == "add-dvfs":
            await rows.mount(self.dvfs_row(DVFSProfile("", 1.0, 1.0, 1.0)))
        elif "dvfs-remove" in event.button.classes:
            await event.button.parent.remove()

    def profiles(self) -> tuple[DVFSProfile, ...]:
        out = []
        for row in self.query("#dvfs-rows > .dvfs-row"):
            name, compute, idle, speed = (box.value for box in row.query(Input))
            out.append(
                DVFSProfile(
                    name.strip(),
                    _number(compute, f"DVFS {name or '?'}: compute power factor"),
                    _number(idle, f"DVFS {name or '?'}: idle power factor"),
                    _number(speed, f"DVFS {name or '?'}: compute speed factor"),
                )
            )
        return tuple(out)

    def config(self) -> PlatformConfig:
        return PlatformConfig(
            num_nodes=_number(self.value("num_nodes"), "Nodes", int),
            node_name=self.value("node_name").strip(),
            base_compute_power=_number(self.value("base_compute_power"), "Compute power"),
            base_idle_power=_number(self.value("base_idle_power"), "Idle power"),
            switching_on_power=_number(self.value("switching_on_power"), "Switching-on power"),
            switching_off_power=_number(self.value("switching_off_power"), "Switching-off power"),
            sleeping_power=_number(self.value("sleeping_power"), "Sleeping power"),
            switching_on_time=_number(self.value("switching_on_time"), "Switching-on time"),
            switching_on_std=_number(self.value("switching_on_std"), "Switching-on std"),
            switching_off_time=_number(self.value("switching_off_time"), "Switching-off time"),
            switching_off_std=_number(self.value("switching_off_std"), "Switching-off std"),
            compute_speed_variation=_number(self.value("compute_speed"), "Compute speed"),
            dvfs_profiles=self.profiles(),
            dvfs_mode=self.value("dvfs_mode").strip(),
        )

    def generate(self) -> str:
        name = _file_name(self.value("name"))
        config = self.config()
        write_platform(generate_platform(config), PLATFORM_DIR / f"{name}.json")
        return (
            f"Wrote platforms/{name}.json: {config.num_nodes} nodes, "
            f"{len(config.dvfs_profiles)} DVFS profile(s)"
        )
