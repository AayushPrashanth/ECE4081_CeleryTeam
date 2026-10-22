"""Core signal processing and persistence for the Celery EMG application.

This module intentionally has no GUI or third-party dependencies, which keeps the
calculation path easy to test and reuse from a packaged Windows executable.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ADC_MIN = 0
ADC_MAX = 4095
DEFAULT_SAMPLE_RATE_HZ = 1000.0
DEFAULT_RMS_WINDOW_SECONDS = 0.25
CHANNELS = ("A0", "A1", "A2", "A3", "A4", "A6")
CHANNEL_CSV_COLUMNS = {
    "A0": "a0_final_adc",
    "A1": "a1_before_notch_adc",
    "A2": "a2_before_notch_adc",
    "A3": "a3_reference_adc",
    "A4": "a4_electrode_1_adc",
    "A6": "a6_electrode_2_adc",
}
CHANNEL_LABELS = {
    "A0": "Final signal",
    "A1": "Before notch filter",
    "A2": "Before notch filter",
    "A3": "Reference electrode",
    "A4": "Electrode 1",
    "A6": "Electrode 2",
}


def app_data_dir() -> Path:
    """Return a user-writable application data directory."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    path = base / "CeleryEMG"
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_adc_line(raw: bytes | str) -> int | None:
    """Parse one firmware line, rejecting malformed and out-of-range values."""
    if isinstance(raw, bytes):
        text = raw.decode("ascii", errors="ignore").strip()
    else:
        text = raw.strip()
    if not text or not text.isdecimal():
        return None
    value = int(text)
    return value if ADC_MIN <= value <= ADC_MAX else None


def parse_sample_line(raw: bytes | str) -> tuple[int, ...] | None:
    """Parse a six-channel firmware frame, with legacy A0-only support."""
    if isinstance(raw, bytes):
        text = raw.decode("ascii", errors="ignore").strip()
    else:
        text = raw.strip()
    if not text:
        return None
    fields = text.split(",")
    if len(fields) not in (1, len(CHANNELS)):
        return None
    values = []
    for raw_field in fields:
        raw_field = raw_field.strip()
        if not raw_field.isdecimal():
            return None
        value = int(raw_field)
        if not ADC_MIN <= value <= ADC_MAX:
            return None
        values.append(value)
    return tuple(values)


def mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def rms(values: Iterable[float]) -> float:
    values_list = list(values)
    if not values_list:
        return 0.0
    return math.sqrt(
        math.fsum(value * value for value in values_list) / len(values_list)
    )


def correct_signal(values: Iterable[float], baseline_adc: float) -> list[float]:
    """Remove the lower-rail-clipped signal baseline and clamp at zero."""
    return [max(0.0, float(value) - baseline_adc) for value in values]


def percent_mvc(
    current_rms: float, mvc_rms_adc: float, cap: float | None = None
) -> float:
    if mvc_rms_adc <= 0:
        return 0.0
    result = max(0.0, current_rms / mvc_rms_adc * 100.0)
    return min(result, cap) if cap is not None else result


def coefficient_of_variation(values: Sequence[float]) -> float:
    """Population coefficient of variation as a percentage."""
    average = mean(values)
    if len(values) < 2 or average <= 0:
        return 0.0
    return statistics.pstdev(values) / average * 100.0


def observed_sample_rate(timestamps: Sequence[float]) -> float:
    if len(timestamps) < 2:
        return 0.0
    elapsed = timestamps[-1] - timestamps[0]
    return (len(timestamps) - 1) / elapsed if elapsed > 0 else 0.0


@dataclass(slots=True)
class Calibration:
    muscle: str
    baseline_adc: float
    rest_rms_adc: float
    mvc_rms_adc: float
    sample_rate_hz: float
    rms_window_seconds: float = DEFAULT_RMS_WINDOW_SECONDS
    mvc_trials: list[float] = field(default_factory=list)
    trial_cv_percent: float = 0.0
    created_at: str = field(default_factory=utc_now_iso)
    signal_note: str = (
        "ADC signal is lower-rail clipped; MVC is a relative within-session metric."
    )
    version: int = 2

    def validate(self) -> None:
        if not self.muscle.strip():
            raise ValueError("A muscle name is required.")
        if not ADC_MIN <= self.baseline_adc <= ADC_MAX:
            raise ValueError("Baseline ADC is outside the 12-bit ADC range.")
        if self.mvc_rms_adc <= 0:
            raise ValueError("MVC reference must be greater than zero.")
        if self.sample_rate_hz < 10:
            raise ValueError("Recorded sample rate is too low.")
        if self.rms_window_seconds <= 0:
            raise ValueError("RMS window must be greater than zero.")

    @property
    def quality_label(self) -> str:
        if len(self.mvc_trials) < 2:
            return "Limited"
        if self.trial_cv_percent <= 15:
            return "Good"
        if self.trial_cv_percent <= 25:
            return "Review"
        return "Repeat recommended"

    def save(self, path: Path) -> None:
        self.validate()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> Calibration:
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Compatibility with the original calibration JSON.
        payload.setdefault(
            "trial_cv_percent", coefficient_of_variation(payload.get("mvc_trials", []))
        )
        payload.setdefault("created_at", utc_now_iso())
        payload.setdefault("version", 1)
        known = {item.name for item in cls.__dataclass_fields__.values()}
        calibration = cls(
            **{key: value for key, value in payload.items() if key in known}
        )
        calibration.validate()
        return calibration


def create_calibration(
    muscle: str,
    rest_values: Sequence[float],
    trial_values: Sequence[Sequence[float]],
    sample_rate_hz: float,
) -> Calibration:
    if not rest_values:
        raise ValueError("No resting samples were captured.")
    if not trial_values or any(not trial for trial in trial_values):
        raise ValueError("Three complete MVC trials are required.")

    baseline = mean(rest_values)
    rest_rms = rms(correct_signal(rest_values, baseline))
    trial_rms = [rms(correct_signal(trial, baseline)) for trial in trial_values]
    if max(trial_rms, default=0.0) <= max(1.0, rest_rms * 2.0):
        raise ValueError("MVC activity was not clearly above the resting signal.")
    trial_cv = coefficient_of_variation(trial_rms)
    if len(trial_rms) >= 2 and trial_cv > 25.0:
        raise ValueError(
            f"MVC trials varied by {trial_cv:.1f}%. Allow adequate recovery and repeat "
            "the three trials before using this calibration."
        )

    # The mean of valid repeated trials is less sensitive to one brief spike.
    calibration = Calibration(
        muscle=muscle,
        baseline_adc=baseline,
        rest_rms_adc=rest_rms,
        mvc_rms_adc=mean(trial_rms),
        sample_rate_hz=sample_rate_hz,
        mvc_trials=trial_rms,
        trial_cv_percent=trial_cv,
    )
    calibration.validate()
    return calibration
