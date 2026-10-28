import copy
import functools
import inspect
from collections.abc import Mapping
from typing import Any

from spars.core.gym import components as G
from spars.core.registry import load_object
from spars.policies.catalog import (
    FEATURE_EXTRACTORS,
    LEARNERS,
    REWARDS,
    TRANSLATORS,
)


CFG: dict[str, Any] = {
    "feature_extractor": "Budiarjo",
    "translator": "Budiarjo",
    "learner": "Budiarjo",
    "reward": {
        "name": "Budiarjo",
        "params": {"weight1": 0.5, "weight2": 0.5, "device": "gpu"},
    },
}


def _resolve_from_map(
    mapping: Mapping[str, Any],
    key_or_object: Any,
) -> Any:
    if callable(key_or_object) and not isinstance(key_or_object, str):
        return key_or_object

    if isinstance(key_or_object, str):
        target = mapping.get(key_or_object, key_or_object)

        if isinstance(target, str):
            return load_object(target)

        return target

    raise TypeError(f"Expected callable or string key/path, got {type(key_or_object)}")


def _resolve_reward(spec: Any) -> Any:
    if isinstance(spec, dict):
        if "name" not in spec:
            raise KeyError("Missing reward configuration field: name")
        if "params" not in spec:
            raise KeyError("Missing reward configuration field: params")

        reward_name = spec["name"]
        reward_parameters = copy.deepcopy(spec["params"])

        if not isinstance(reward_parameters, dict):
            raise TypeError("reward params must be a dictionary")

        if isinstance(reward_name, str):
            reward_name = REWARDS.get(
                reward_name,
                reward_name,
            )

        return G.make_reward(
            {
                "name": reward_name,
                "params": reward_parameters,
            }
        )

    if isinstance(spec, str):
        spec = REWARDS.get(spec, spec)

    return G.make_reward(spec)


def _reward_factory() -> Any:
    return _resolve_reward(CFG["reward"])


feature_extractor = _resolve_from_map(
    FEATURE_EXTRACTORS,
    CFG["feature_extractor"],
)
translator = _resolve_from_map(
    TRANSLATORS,
    CFG["translator"],
)
learner = _resolve_from_map(
    LEARNERS,
    CFG["learner"],
)
# Construct rewards only after the run supplies its effective device/settings.
reward_instance = None


# Runtime configuration
def apply_feature_extractor_config(
    feature_extractor_cfg: Any,
) -> Any:
    global feature_extractor

    feature_extractor = _resolve_from_map(
        FEATURE_EXTRACTORS,
        feature_extractor_cfg,
    )
    CFG["feature_extractor"] = copy.deepcopy(feature_extractor_cfg)
    G.feature_extraction = feature_extractor

    return feature_extractor


def apply_translator_config(
    translator_cfg: Any,
) -> Any:
    global translator

    translator = _resolve_from_map(
        TRANSLATORS,
        translator_cfg,
    )
    CFG["translator"] = copy.deepcopy(translator_cfg)
    G.action_translator = translator

    return translator


def apply_reward_config(
    reward_cfg: dict[str, Any],
) -> Any:
    global reward_instance

    if not isinstance(reward_cfg, dict):
        raise TypeError("reward configuration must be a dictionary")

    if "name" not in reward_cfg:
        raise KeyError("Missing reward configuration field: name")

    if "params" not in reward_cfg:
        raise KeyError("Missing reward configuration field: params")

    if not isinstance(reward_cfg["params"], dict):
        raise TypeError("reward params must be a dictionary")

    CFG["reward"] = copy.deepcopy(reward_cfg)
    reward_instance = _reward_factory()
    G.Reward = _reward_factory

    return reward_instance


# Arguments the training loop supplies; a config cannot bind them.
_LEARNER_DATA_ARGUMENTS = {
    "model",
    "model_opt",
    "saved_experiences",
    "next_value",
}


def _bind_learner_params(function: Any, params: Mapping[str, Any]) -> Any:
    """Bind configured keyword settings, rejecting unknown or data arguments."""
    if not isinstance(params, dict):
        raise TypeError("learner params must be a dictionary")
    known = inspect.signature(function).parameters
    invalid = sorted(set(params) - set(known) | set(params) & _LEARNER_DATA_ARGUMENTS)
    if invalid:
        raise ValueError(f"Unknown or non-configurable learner params: {invalid}")
    if not params:
        return function
    bound = functools.partial(function, **copy.deepcopy(dict(params)))
    # Contract code reads the module (LEARNER_SCHEMA) and the signature.
    bound.__module__ = function.__module__
    return bound


def apply_learner_config(learner_cfg: Any) -> Any:
    """Accept a learner name/object, or {"name": ..., "params": {...}}."""
    global learner

    if isinstance(learner_cfg, dict):
        if "name" not in learner_cfg:
            raise KeyError("Missing learner configuration field: name")
        learner = _bind_learner_params(
            _resolve_from_map(LEARNERS, learner_cfg["name"]),
            learner_cfg.get("params", {}),
        )
    else:
        learner = _resolve_from_map(
            LEARNERS,
            learner_cfg,
        )
    CFG["learner"] = copy.deepcopy(learner_cfg)
    G.learn = learner

    return learner


def apply_gym_config(
    gym_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(gym_cfg, dict):
        raise TypeError("gym configuration must be a dictionary")

    required_fields = (
        "feature_extractor",
        "translator",
        "reward",
        "learner",
    )
    missing_fields = [field for field in required_fields if field not in gym_cfg]
    if missing_fields:
        raise KeyError(f"Missing gym configuration fields: {missing_fields}")

    apply_feature_extractor_config(gym_cfg["feature_extractor"])
    apply_translator_config(gym_cfg["translator"])
    apply_reward_config(gym_cfg["reward"])
    apply_learner_config(gym_cfg["learner"])

    return CFG


G.feature_extraction = feature_extractor
G.action_translator = translator
G.Reward = _reward_factory
G.learn = learner

SELECTED = CFG
