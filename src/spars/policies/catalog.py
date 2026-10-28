"""Names the experiment definitions use for the built-in policies.

Each name maps to the "module:Class" (or "module:function") that implements
it. The core loads a policy from here by name; a definition may also give a
"module:Class" path directly to use a policy that is not listed.
"""

_SCHEDULER = "spars.policies.scheduler"
_POWER = "spars.policies.power_management"
_RL = f"{_POWER}.rl"

SCHEDULERS = {
    # Short names: "fcfs" and "easy" run the PSAS implementations.
    "fcfs": f"{_SCHEDULER}.fcfs.psas:FCFSPSAS",
    "easy": f"{_SCHEDULER}.easy.psas:EASYPSAS",
    "fcfs_baseline": f"{_SCHEDULER}.fcfs.baseline:FCFSBaseline",
    "easy_baseline": f"{_SCHEDULER}.easy.baseline:EASYBaseline",
    "fcfs_psus": f"{_SCHEDULER}.fcfs.psus:FCFSPSUS",
    "fcfs_psas": f"{_SCHEDULER}.fcfs.psas:FCFSPSAS",
    "easy_psus": f"{_SCHEDULER}.easy.psus:EASYPSUS",
    "easy_psas": f"{_SCHEDULER}.easy.psas:EASYPSAS",
    "snf_psas": f"{_SCHEDULER}.snf.psas:SNFPSAS",
    "snf_baseline": f"{_SCHEDULER}.snf.baseline:SNFBaseline",
    "snf_oracle_psas": f"{_SCHEDULER}.snf.oracle:SNFOraclePSAS",
    "snf_icon": f"{_SCHEDULER}.snf.icon:SNFICON",
}

POWER_MANAGERS = {
    "timeout": f"{_POWER}.timeout.policy:TimeoutPowerManager",
    "psus_timeout": f"{_POWER}.timeout.psus:PSUSTimeoutPowerManager",
    "icon": f"{_POWER}.icon:ICONPowerManager",
    "rl": f"{_RL}.controller:RLPowerManager",
    "oracle": f"{_POWER}.oracle:OraclePowerManager",
    "none": f"{_POWER}.none:NoPowerManager",
}

# RL components, selected by the gym section of a definition.
FEATURE_EXTRACTORS = {
    "Budiarjo": f"{_RL}.budiarjo.features:feature_extraction",
}
TRANSLATORS = {
    "Budiarjo": f"{_RL}.budiarjo.translator:action_translator",
}
REWARDS = {"Budiarjo": f"{_RL}.reward:Reward"}
LEARNERS = {
    "Budiarjo": f"{_RL}.budiarjo.learner:learn",
}

# Old definitions name one run.algorithm whose algo_config also holds the
# power manager's settings. Each algorithm maps to (power manager, algo_config
# keys that move to its config when present, algo_config keys that are
# dropped); others use DEFAULT_ALGORITHM_POWER_MANAGER.
ALGORITHM_POWER_MANAGERS = {
    "snf_icon": (
        "icon",
        (
            "alpha",
            "beta",
            "min_spare_nodes",
            "max_spare_nodes",
            "spare_arrival_window",
            "immediate_shutdown_on_stale_completion",
            "decision_log_enabled",
            "decision_log_path",
            "timeout",
            "respect_timeout",
        ),
        (),
    ),
    "snf_oracle_psas": ("oracle", (), ()),
    # The baselines run without a power manager.
    "fcfs_baseline": ("none", (), ("timeout",)),
    "easy_baseline": ("none", (), ("timeout",)),
    "snf_baseline": ("none", (), ("timeout",)),
    "fcfs_psus": ("psus_timeout", ("timeout",), ()),
    "easy_psus": ("psus_timeout", ("timeout",), ()),
}
DEFAULT_ALGORITHM_POWER_MANAGER = ("timeout", ("timeout",), ())

# Config keys (with defaults) of policies whose class has no DEFAULTS attribute
# and that accept settings; the dashboard form builds its fields from these
# and from the DEFAULTS of the policy classes that have them.
DEFAULT_CONFIGS = {
    "timeout": {"timeout": 3600},
    "psus_timeout": {"timeout": 3600},
}
