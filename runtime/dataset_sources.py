"""Resolve a dataset root from a selected telemetry directory."""

from __future__ import annotations

from pathlib import Path


def catalogue_root(dataset: str, telemetry_path: Path) -> Path:
    """Locate incident labels above an extracted dataset's telemetry folder."""
    candidate = telemetry_path.resolve()
    for parent in (candidate, *candidate.parents):
        if dataset == "nezha" and (parent / "rca_data").is_dir():
            return parent
        if dataset in {"openrca_bank", "openrca_telecom"}:
            expected = dataset.removeprefix("openrca_")
            if parent.name.lower() == expected and (parent / "record.csv").is_file():
                return parent
        if dataset == "openrca_market" and parent.name.lower() == "market":
            if len(list(parent.glob("cloudbed-*/record.csv"))) == 2:
                return parent
    return candidate
