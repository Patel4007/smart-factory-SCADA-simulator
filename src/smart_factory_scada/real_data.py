from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np


DATASET_NAME = "Bosch CNC Machining Dataset"
DATASET_DETAIL = "Real Bosch CNC vibration traces from M01/M02/M03 operations OP01, OP03, and OP05."
DATASET_SOURCE_URL = "https://github.com/boschresearch/CNC_Machining"
DEFAULT_SAMPLE_RATE_HZ = 2000
SUPPORTED_TRACE_SUFFIXES = {
    ".h5": "hdf5",
    ".hdf5": "hdf5",
    ".csv": "csv",
    ".txt": "csv",
}


@dataclass(frozen=True, slots=True)
class TraceWindow:
    index: int
    rms_magnitude: float
    peak_magnitude: float
    crest_factor: float
    intensity_ratio: float


@dataclass(frozen=True, slots=True)
class RealTrace:
    trace_id: str
    machine_code: str
    operation_code: str
    quality_label: str
    file_name: str
    source_path: str
    source_format: str
    sample_rate_hz: int
    sample_count: int
    mean_rms: float
    mean_peak: float
    windows: tuple[TraceWindow, ...]

    def window_at(self, cursor: int) -> TraceWindow:
        if not self.windows:
            raise ValueError(f"Trace {self.trace_id} contains no windows.")
        return self.windows[cursor % len(self.windows)]


def load_curated_bosch_sample(data_dir: str | Path, *, window_samples: int = DEFAULT_SAMPLE_RATE_HZ) -> dict[str, RealTrace]:
    root = Path(data_dir)
    catalog = {
        "m01_op01_good": root / "M01" / "OP01" / "good" / "M01_Aug_2019_OP01_000.h5",
        "m01_op01_bad": root / "M01" / "OP01" / "bad" / "M01_Aug_2019_OP01_000.h5",
        "m02_op03_good": root / "M02" / "OP03" / "good" / "M02_Aug_2019_OP03_000.h5",
        "m03_op05_good": root / "M03" / "OP05" / "good" / "M03_Aug_2019_OP05_000.h5",
    }
    traces: dict[str, RealTrace] = {}
    for trace_id, path in catalog.items():
        traces[trace_id] = load_trace_file(trace_id, path, window_samples=window_samples)
    return traces


def load_trace_file(
    trace_id: str,
    path: str | Path,
    *,
    window_samples: int,
    sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
    machine_code: str | None = None,
    operation_code: str | None = None,
    quality_label: str | None = None,
) -> RealTrace:
    file_path = Path(path)
    vibration_data, source_format = _read_vibration_data(file_path)
    return _build_trace(
        trace_id=trace_id,
        path=file_path,
        vibration_data=vibration_data,
        source_format=source_format,
        window_samples=window_samples,
        sample_rate_hz=sample_rate_hz,
        machine_code=machine_code,
        operation_code=operation_code,
        quality_label=quality_label,
    )


def _build_trace(
    *,
    trace_id: str,
    path: Path,
    vibration_data: np.ndarray,
    source_format: str,
    window_samples: int,
    sample_rate_hz: int,
    machine_code: str | None,
    operation_code: str | None,
    quality_label: str | None,
) -> RealTrace:
    if not path.exists():
        raise FileNotFoundError(f"Real dataset file is missing: {path}")

    segments = _split_windows(vibration_data, window_samples=window_samples)
    raw_windows: list[tuple[float, float, float]] = []
    for segment in segments:
        centered = segment - segment.mean(axis=0, keepdims=True)
        magnitude = np.linalg.norm(centered, axis=1)
        rms_magnitude = float(np.sqrt(np.mean(magnitude**2)))
        peak_magnitude = float(np.max(magnitude))
        crest_factor = peak_magnitude / max(rms_magnitude, 1e-9)
        raw_windows.append((rms_magnitude, peak_magnitude, crest_factor))

    mean_rms = float(np.mean([item[0] for item in raw_windows]))
    mean_peak = float(np.mean([item[1] for item in raw_windows]))
    windows = tuple(
        TraceWindow(
            index=index,
            rms_magnitude=rms_magnitude,
            peak_magnitude=peak_magnitude,
            crest_factor=crest_factor,
            intensity_ratio=rms_magnitude / max(mean_rms, 1e-9),
        )
        for index, (rms_magnitude, peak_magnitude, crest_factor) in enumerate(raw_windows)
    )

    resolved_machine_code = machine_code or _path_part(path.parts, -4, "UPLOAD")
    resolved_operation_code = operation_code or _path_part(path.parts, -3, "CUSTOM")
    resolved_quality = quality_label or _path_part(path.parts, -2, "good")
    return RealTrace(
        trace_id=trace_id,
        machine_code=resolved_machine_code,
        operation_code=resolved_operation_code,
        quality_label=resolved_quality,
        file_name=path.name,
        source_path=str(path),
        source_format=source_format,
        sample_rate_hz=sample_rate_hz,
        sample_count=int(vibration_data.shape[0]),
        mean_rms=mean_rms,
        mean_peak=mean_peak,
        windows=windows,
    )


def _read_vibration_data(path: Path) -> tuple[np.ndarray, str]:
    source_format = SUPPORTED_TRACE_SUFFIXES.get(path.suffix.lower())
    if source_format is None:
        supported = ", ".join(sorted(SUPPORTED_TRACE_SUFFIXES))
        raise ValueError(f"Unsupported trace format: {path.suffix or '<none>'}. Supported formats: {supported}.")
    if source_format == "hdf5":
        return _read_hdf5_matrix(path), source_format
    return _read_csv_matrix(path), source_format


def _read_hdf5_matrix(path: Path) -> np.ndarray:
    with h5py.File(path, "r") as handle:
        if "vibration_data" in handle:
            dataset = handle["vibration_data"]
        else:
            dataset_name = _discover_numeric_dataset(handle)
            if dataset_name is None:
                raise ValueError(f"No numeric vibration dataset was found in {path.name}.")
            dataset = handle[dataset_name]
        matrix = np.asarray(dataset[:], dtype=np.float64)
    return _normalize_matrix(matrix, source_name=path.name)


def _discover_numeric_dataset(handle: h5py.File) -> str | None:
    candidates: list[str] = []

    def visitor(name: str, obj: h5py.Dataset | h5py.Group) -> None:
        if isinstance(obj, h5py.Dataset) and np.issubdtype(obj.dtype, np.number):
            candidates.append(name)

    handle.visititems(visitor)
    return candidates[0] if candidates else None


def _read_csv_matrix(path: Path) -> np.ndarray:
    attempts = [
        np.genfromtxt(path, delimiter=",", dtype=np.float64, invalid_raise=False),
        np.genfromtxt(path, dtype=np.float64, invalid_raise=False),
    ]
    for attempt in attempts:
        matrix = _strip_nan_rows_and_columns(np.asarray(attempt, dtype=np.float64))
        if matrix.size:
            return _normalize_matrix(matrix, source_name=path.name)
    raise ValueError(f"No numeric vibration samples were found in {path.name}.")


def _strip_nan_rows_and_columns(matrix: np.ndarray) -> np.ndarray:
    if matrix.ndim == 0:
        return np.asarray([])
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    row_mask = ~np.all(np.isnan(matrix), axis=1)
    matrix = matrix[row_mask]
    if matrix.size == 0:
        return matrix
    column_mask = ~np.all(np.isnan(matrix), axis=0)
    matrix = matrix[:, column_mask]
    return np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)


def _normalize_matrix(matrix: np.ndarray, *, source_name: str) -> np.ndarray:
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    elif matrix.ndim > 2:
        matrix = matrix.reshape(matrix.shape[0], -1)
    if matrix.shape[0] == 0 or matrix.shape[1] == 0:
        raise ValueError(f"{source_name} does not contain any usable vibration samples.")
    if matrix.shape[0] < matrix.shape[1]:
        matrix = matrix.T
    if matrix.shape[1] > 3:
        matrix = matrix[:, :3]
    return matrix


def _split_windows(vibration_data: np.ndarray, *, window_samples: int) -> list[np.ndarray]:
    if len(vibration_data) <= window_samples:
        return [vibration_data]
    windows = [
        vibration_data[offset : offset + window_samples]
        for offset in range(0, len(vibration_data) - window_samples + 1, window_samples)
    ]
    return windows or [vibration_data]


def _path_part(parts: Iterable[str], index: int, default: str) -> str:
    parts_tuple = tuple(parts)
    try:
        return parts_tuple[index]
    except IndexError:
        return default
