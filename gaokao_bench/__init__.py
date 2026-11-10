"""Gaokao-Bench data loading and deterministic scoring."""

from .scoring import DATASET_VERSION, Dataset, evaluate_submission, load_dataset
from .version import ENGINE_VERSION

__version__ = ENGINE_VERSION

__all__ = ["DATASET_VERSION", "Dataset", "evaluate_submission", "load_dataset", "__version__"]

