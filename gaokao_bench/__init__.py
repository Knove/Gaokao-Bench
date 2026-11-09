"""Gaokao-Bench data loading and deterministic scoring."""

from .scoring import DATASET_VERSION, Dataset, evaluate_submission, load_dataset

__all__ = ["DATASET_VERSION", "Dataset", "evaluate_submission", "load_dataset"]

