"""Single source of truth for engine and schema versions."""

ENGINE_VERSION = "0.1.4"
HARNESS_VERSION = "0.1.4"
SCORER_VERSION = "0.1.2"
SUMMARY_SCHEMA_VERSION = "2.2"
DATA_SCHEMA_VERSION = "1.0"
BENCHMARK_SPEC_SCHEMA_VERSION = "1.0"
DEFAULT_DATASET_VERSION = "0.0.4"
DEFAULT_BENCHMARK_SPEC_VERSION = "1.2"
DEFAULT_METRIC_VERSION = "1.0"


def compatibility_matrix(
    *,
    dataset_version: str,
    benchmark_spec_version: str,
    metric_version: str = "1.0",
) -> dict:
    """Return the release-facing compatibility matrix.

    Version numbers are intentionally explicit.  Consumers should cite all of
    them, not infer a dataset version from the engine version or vice versa.
    """

    return {
        "engine": ENGINE_VERSION,
        "harness": HARNESS_VERSION,
        "scorer": SCORER_VERSION,
        "dataset": dataset_version,
        "benchmark_spec": benchmark_spec_version,
        "benchmark_spec_schema": BENCHMARK_SPEC_SCHEMA_VERSION,
        "data_schema": DATA_SCHEMA_VERSION,
        "summary_schema": SUMMARY_SCHEMA_VERSION,
        "metric_version": metric_version,
        "minimum_engine": ENGINE_VERSION,
        "release_card_schema": "1.1",
    }
