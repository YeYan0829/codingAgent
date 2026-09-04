"""外部 benchmark 的薄适配层。"""

from codeagent.benchmark.swebench_source import (
    PreparedSWEbenchSource,
    SWEbenchPreparationError,
    SWEbenchSourcePreparer,
    SWEbenchTaskSource,
)
from codeagent.benchmark.swebench_executor import (
    DockerCLIExecutionBackend,
    SWEbenchDockerCommandExecutor,
)
from codeagent.benchmark.swebench_harness import (
    OracleResult,
    SWEbenchHarness,
    SWEbenchCLIGrader,
    SWEbenchRunResult,
    SWEbenchTask,
)
from codeagent.benchmark.swebench_preflight import (
    GoldPreflightResult,
    GoldTaskResult,
    SWEbenchGoldPreflight,
    SWEbenchTaskRecord,
    SWEbenchTaskRepository,
    SWEbenchTaskRepositoryError,
)
from codeagent.benchmark.accounting import PriceSnapshot, aggregate_usage, calculate_cost
from codeagent.benchmark.batch import BatchCompatibilityError, SWEbenchBatchRunner, SWEbenchSelection
from codeagent.benchmark.trajectory import analyze_trajectory, load_events, save_trajectory

__all__ = [
    "PreparedSWEbenchSource",
    "SWEbenchPreparationError",
    "SWEbenchSourcePreparer",
    "SWEbenchTaskSource",
    "DockerCLIExecutionBackend",
    "SWEbenchDockerCommandExecutor",
    "OracleResult",
    "SWEbenchHarness",
    "SWEbenchCLIGrader",
    "SWEbenchRunResult",
    "SWEbenchTask",
    "GoldPreflightResult",
    "GoldTaskResult",
    "SWEbenchGoldPreflight",
    "SWEbenchTaskRecord",
    "SWEbenchTaskRepository",
    "SWEbenchTaskRepositoryError",
    "PriceSnapshot", "aggregate_usage", "calculate_cost",
    "BatchCompatibilityError", "SWEbenchBatchRunner", "SWEbenchSelection",
    "analyze_trajectory", "load_events", "save_trajectory",
]
