import importlib
from collections.abc import Mapping
from inspect import Parameter, signature
from typing import Any, TypeVar

from spars.policies.discovery import resolve
from spars.policies.catalog import (
    DEFAULT_ALGORITHM_POWER_MANAGER,
    ALGORITHM_POWER_MANAGERS,
)


Component = TypeVar("Component")


def validate_component_config(
    config: dict[str, Any],
    component_name: str,
    expected_keys: set[str],
) -> dict[str, Any]:
    """Validate and return a component's single config object."""
    if not isinstance(config, dict):
        raise TypeError(
            f"config for {component_name!r} must be a dictionary"
        )

    missing_keys = sorted(expected_keys - set(config))
    unexpected_keys = sorted(set(config) - expected_keys)
    if missing_keys or unexpected_keys:
        details = []
        if missing_keys:
            details.append(f"missing keys: {missing_keys}")
        if unexpected_keys:
            details.append(f"unexpected keys: {unexpected_keys}")
        raise ValueError(
            f"Invalid config for {component_name!r}: "
            + "; ".join(details)
        )

    return config


def config_with_defaults(
    config: dict[str, Any],
    component_name: str,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    """A component's config, with ``defaults`` for the keys it leaves out."""
    if not isinstance(config, dict):
        raise TypeError(
            f"config for {component_name!r} must be a dictionary"
        )

    unexpected_keys = sorted(set(config) - set(defaults))
    if unexpected_keys:
        raise ValueError(
            f"Invalid config for {component_name!r}: "
            f"unexpected keys: {unexpected_keys}"
        )

    return {**defaults, **config}


def resolve_run_components(
    run_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return scheduler and power-management specs for a run config."""
    if "scheduler" in run_config or "power_management" in run_config:
        if "scheduler" not in run_config or "power_management" not in run_config:
            raise KeyError(
                "run.scheduler and run.power_management must be configured "
                "together"
            )

        scheduler = run_config["scheduler"]
        power_management = run_config["power_management"]
        for component_name, component in (
            ("scheduler", scheduler),
            ("power_management", power_management),
        ):
            if not isinstance(component, dict):
                raise TypeError(
                    f"run.{component_name} must be a dictionary"
                )
            if "name" not in component or "config" not in component:
                raise KeyError(
                    f"run.{component_name} requires name and config"
                )
            if not isinstance(component["name"], str) or not component["name"]:
                raise ValueError(
                    f"run.{component_name}.name must be a non-empty string"
                )
            if not isinstance(component["config"], dict):
                raise TypeError(
                    f"run.{component_name}.config must be a dictionary"
                )

        return (
            {
                "name": policy_name(scheduler["name"]),
                "config": dict(scheduler["config"]),
            },
            {
                "name": policy_name(power_management["name"]),
                "config": dict(power_management["config"]),
            },
        )

    scheduler_name = policy_name(str(run_config["algorithm"]))
    scheduler_config = dict(run_config["algo_config"])
    power_name, power_keys, dropped_keys = ALGORITHM_POWER_MANAGERS.get(
        scheduler_name, DEFAULT_ALGORITHM_POWER_MANAGER
    )
    power_management = {
        "name": power_name,
        "config": {
            key: scheduler_config.pop(key)
            for key in power_keys
            if key in scheduler_config
        },
    }
    for key in dropped_keys:
        scheduler_config.pop(key, None)

    return (
        {
            "name": scheduler_name,
            "config": scheduler_config,
        },
        power_management,
    )


def get_run_timeout(run_config: dict[str, Any]) -> Any:
    """Return the effective timeout used for run labels and metadata."""
    if "scheduler" not in run_config and "power_management" not in run_config:
        return run_config["algo_config"].get("timeout")

    scheduler, power_management = resolve_run_components(run_config)
    if "timeout" in power_management["config"]:
        return power_management["config"]["timeout"]
    return None


def build_component(
    component_class: type[Component],
    runtime_values: dict[str, Any],
    config: dict[str, Any],
    component_name: str,
) -> Component:
    """Instantiate a component with runtime values and one config object."""
    constructor_parameters = signature(component_class.__init__).parameters
    parameter_names = set(constructor_parameters) - {"self"}

    for parameter_name, parameter in constructor_parameters.items():
        if parameter_name == "self":
            continue

        if parameter_name == "config":
            continue

        if parameter.kind in (
            Parameter.VAR_POSITIONAL,
            Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                f"{component_class.__name__}.__init__ cannot use "
                "*args or **kwargs"
            )

        if (
            parameter_name not in runtime_values
            and parameter.default is Parameter.empty
        ):
            raise TypeError(
                f"Missing runtime value {parameter_name!r} for "
                f"{component_class.__name__}"
            )

    if "config" not in parameter_names:
        raise TypeError(
            f"{component_class.__name__}.__init__ must accept a config object"
        )

    if not isinstance(config, dict):
        raise TypeError(
            f"config for {component_name!r} must be a dictionary"
        )

    return component_class(
        **{
            name: value
            for name, value in runtime_values.items()
            if name in parameter_names and name != "config"
        },
        config=config,
    )


def load_object(import_path: str) -> Any:
    """Load ``pkg.mod:obj`` or ``pkg.mod.obj`` into a Python object."""
    if ":" in import_path:
        module_name, object_name = import_path.split(":", 1)
    else:
        module_name, _, object_name = import_path.rpartition(".")
        if not module_name:
            raise ValueError(
                f"Cannot import {import_path!r}. Use 'pkg.mod:obj' or 'pkg.mod.obj'."
            )

    module = importlib.import_module(module_name)
    return getattr(module, object_name)


def policy_name(name: str) -> str:
    """Catalog names ignore case; a "module:Class" path is kept as given."""
    return name if ":" in name else name.lower()


def load_policy(kind: str, name: str) -> Any:
    """Return the class for a catalog alias, a class name or a "module:Class" path.

    ``kind`` is "scheduler" or "power_management"; see spars.policies.discovery.
    """
    return load_object(resolve(kind, name))
