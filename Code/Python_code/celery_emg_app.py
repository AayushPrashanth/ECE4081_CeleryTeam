"""Integrated clinician-facing EMG biofeedback desktop application."""

from __future__ import annotations

import csv
import math
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from emg_core import (
    ADC_MAX,
    CHANNEL_CSV_COLUMNS,
    CHANNEL_LABELS,
    CHANNELS,
    Calibration,
    app_data_dir,
    create_calibration,
    observed_sample_rate,
    parse_sample_line,
    percent_mvc,
    rms,
)

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # A clear GUI error is shown from main().
    serial = None
    list_ports = None


APP_NAME = "Celery EMG"
BAUD_RATE = 460800
REST_SECONDS = 3.0
MVC_SECONDS = 3.0
MVC_STABLE_START = 0.5
MVC_STABLE_END = 2.5
MVC_REST_SECONDS = 30
MVC_TRIAL_COUNT = 3

BG = "#F2F6F5"
SURFACE = "#FFFFFF"
INK = "#162924"
MUTED = "#60736D"
PRIMARY = "#13795B"
PRIMARY_DARK = "#0A5B43"
MINT = "#DDF4EB"
TEAL = "#28A47C"
AMBER = "#E89B2D"
RED = "#D65252"
BORDER = "#D8E3DF"
NAV = "#10362D"

MUSCLE_PROFILES = {
    "Biceps": "Keep the elbow near 90° and flex against immovable resistance.",
    "Triceps": "Keep the elbow fixed and extend against immovable resistance.",
    "Deltoid": "Attempt shoulder abduction against immovable resistance.",
    "Quadriceps": "Keep the knee fixed and attempt maximum knee extension.",
    "Hamstrings": "Keep the knee fixed and attempt maximum knee flexion.",
}


class SerialSource:
    """Continuously read newline-delimited 12-bit ADC samples off the UI thread."""

    def __init__(self, port: str):
        self.port = port
        self.samples: queue.Queue[tuple[float, tuple[int, ...]]] = queue.Queue(
            maxsize=100_000
        )
        self.error: str | None = None
        self.last_sample_at = 0.0
        self._serial = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self._serial = serial.Serial(self.port, BAUD_RATE, timeout=0.1)
        self._serial.reset_input_buffer()
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                frame = parse_sample_line(self._serial.readline())
                if frame is None:
                    continue
                timestamp = time.monotonic()
                self.last_sample_at = timestamp
                try:
                    self.samples.put_nowait((timestamp, frame))
                except queue.Full:
                    self.error = (
                        "Input buffer overflowed; stop and reconnect the device."
                    )
                    break
        except (OSError, serial.SerialException) as exc:
            if not self._stop.is_set():
                self.error = str(exc)

    def drain(self, limit: int = 10_000) -> list[tuple[float, tuple[int, ...]]]:
        result = []
        for _ in range(limit):
            try:
                result.append(self.samples.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self) -> None:
        self._stop.set()
        if self._serial is not None:
            try:
                self._serial.close()
            except (OSError, serial.SerialException):
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)


class DemoSource:
    """Synthetic source for training, demonstrations, and no-hardware testing."""

    def __init__(self):
        self.samples: queue.Queue[tuple[float, tuple[int, ...]]] = queue.Queue(
            maxsize=100_000
        )
        self.error = None
        self.last_sample_at = 0.0
        self.mode = "session"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._generate, daemon=True)
        self._thread.start()

    def _generate(self) -> None:
        batch_period = 0.010
        samples_per_batch = 10
        while not self._stop.is_set():
            batch_end = time.monotonic()
            for index in range(samples_per_batch):
                timestamp = batch_end - (samples_per_batch - index - 1) / 1000.0
                self._phase += 0.018
                if self.mode == "rest":
                    value = 100 + random.gauss(0, 5)
                elif self.mode == "contract":
                    value = 100 + abs(random.gauss(410, 100))
                else:
                    envelope = 0.50 + 0.32 * math.sin(self._phase * 0.12)
                    value = 100 + abs(random.gauss(620 * envelope, 70))
                a0 = max(0, min(ADC_MAX, round(value)))
                frame = (
                    a0,
                    max(0, min(ADC_MAX, round(a0 * 0.92 + random.gauss(0, 18)))),
                    max(0, min(ADC_MAX, round(a0 * 0.88 + random.gauss(0, 25)))),
                    max(0, min(ADC_MAX, round(1650 + random.gauss(0, 10)))),
                    max(
                        0,
                        min(ADC_MAX, round(1500 + value * 0.42 + random.gauss(0, 20))),
                    ),
                    max(
                        0,
                        min(ADC_MAX, round(1500 - value * 0.38 + random.gauss(0, 20))),
                    ),
                )
                self.last_sample_at = timestamp
                try:
                    self.samples.put_nowait((timestamp, frame))
                except queue.Full:
                    self.error = "Demo sample buffer overflowed."
                    return
            time.sleep(batch_period)

    def drain(self, limit: int = 10_000) -> list[tuple[float, tuple[int, ...]]]:
        result = []
        for _ in range(limit):
            try:
                result.append(self.samples.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)


def make_source(port: str, demo: bool):
    return DemoSource() if demo else SerialSource(port)


class CalibrationDialog(tk.Toplevel):
    def __init__(self, parent, port: str, muscle: str, demo: bool, on_complete):
        super().__init__(parent)
        self.port = port
        self.muscle = muscle
        self.demo = demo
        self.on_complete = on_complete
        self.source = make_source(port, demo)
        self.rest_samples: list[float] = []
        self.rest_timestamps: list[float] = []
        self.trials: list[list[float]] = []
        self.capture_start = 0.0
        self.capture_duration = 0.0
        self.capture_values: list[tuple[float, tuple[int, ...]]] = []
        self.cancelled = False
        self.after_id = None

        self.title("Guided MVC calibration")
        self.geometry("720x560")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self._build()
        try:
            self.source.start()
        except (OSError, ValueError, RuntimeError, serial.SerialException) as exc:
            messagebox.showerror("Could not connect", str(exc), parent=self)
            self.after(0, self.cancel)

    def _build(self):
        header = tk.Frame(self, bg=NAV, height=112)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="GUIDED CALIBRATION",
            bg=NAV,
            fg="#A9D9CA",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=34, pady=(22, 3))
        tk.Label(
            header, text=self.muscle, bg=NAV, fg="white", font=("Segoe UI", 25, "bold")
        ).pack(anchor="w", padx=34)

        card = tk.Frame(
            self, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER
        )
        card.pack(fill="both", expand=True, padx=32, pady=28)
        self.step_label = tk.Label(
            card,
            text="STEP 1 OF 4",
            bg=SURFACE,
            fg=PRIMARY,
            font=("Segoe UI", 10, "bold"),
        )
        self.step_label.pack(anchor="w", padx=28, pady=(26, 8))
        self.title_label = tk.Label(
            card,
            text="Resting baseline",
            bg=SURFACE,
            fg=INK,
            font=("Segoe UI", 22, "bold"),
        )
        self.title_label.pack(anchor="w", padx=28)
        self.instruction_label = tk.Label(
            card,
            text="Ask the patient to relax the target muscle completely. Keep the electrodes still.",
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 11),
            wraplength=590,
            justify="left",
        )
        self.instruction_label.pack(anchor="w", padx=28, pady=(12, 18))
        self.progress = ttk.Progressbar(card, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=28, pady=(4, 10))
        self.countdown_label = tk.Label(
            card, text="Ready", bg=SURFACE, fg=INK, font=("Segoe UI", 15, "bold")
        )
        self.countdown_label.pack(anchor="w", padx=28, pady=(0, 20))
        footer = tk.Frame(card, bg=SURFACE)
        footer.pack(side="bottom", fill="x", padx=28, pady=26)
        tk.Button(
            footer,
            text="Cancel",
            command=self.cancel,
            bg="#EEF3F1",
            fg=INK,
            relief="flat",
            padx=18,
            pady=10,
            font=("Segoe UI", 10),
        ).pack(side="left")
        self.action = tk.Button(
            footer,
            text="Record resting baseline",
            command=self.start_rest,
            bg=PRIMARY,
            activebackground=PRIMARY_DARK,
            fg="white",
            activeforeground="white",
            relief="flat",
            padx=22,
            pady=10,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.action.pack(side="right")

    def start_rest(self):
        self.action.configure(state="disabled")
        if isinstance(self.source, DemoSource):
            self.source.mode = "rest"
        self._begin_capture(REST_SECONDS, self._finish_rest)

    def _begin_capture(self, duration: float, callback):
        self.source.drain()
        self.capture_start = time.monotonic()
        self.capture_duration = duration
        self.capture_values = []
        self.capture_callback = callback
        self.progress["value"] = 0
        self._capture_tick()

    def _capture_tick(self):
        if self.cancelled:
            return
        self.capture_values.extend(self.source.drain())
        elapsed = time.monotonic() - self.capture_start
        remaining = max(0.0, self.capture_duration - elapsed)
        self.progress["value"] = min(100, elapsed / self.capture_duration * 100)
        self.countdown_label.configure(text=f"Recording · {remaining:0.1f} s")
        if self.source.error:
            messagebox.showerror("Device error", self.source.error, parent=self)
            self.cancel()
            return
        if elapsed >= self.capture_duration:
            self.capture_values.extend(self.source.drain())
            self.capture_callback()
            return
        self.after_id = self.after(30, self._capture_tick)

    def _enough_samples(self, values) -> bool:
        # Allows slower firmware while rejecting a disconnected or nearly empty stream.
        minimum = max(30, int(self.capture_duration * 100))
        if len(values) >= minimum:
            return True
        messagebox.showerror(
            "Not enough signal data",
            f"Only {len(values)} valid samples were received. Check the electrode unit and cable, then retry.",
            parent=self,
        )
        return False

    def _finish_rest(self):
        if not self._enough_samples(self.capture_values):
            self.countdown_label.configure(text="Resting capture failed")
            self.action.configure(text="Retry resting baseline", state="normal")
            return
        self.rest_timestamps = [stamp for stamp, _ in self.capture_values]
        self.rest_samples = [frame[0] for _, frame in self.capture_values]
        self._prepare_trial(1)

    def _prepare_trial(self, number: int):
        self.step_label.configure(text=f"STEP {number + 1} OF 4")
        self.title_label.configure(text=f"MVC trial {number} of {MVC_TRIAL_COUNT}")
        self.instruction_label.configure(
            text=MUSCLE_PROFILES[self.muscle]
            + " The patient should stop if they feel pain, dizziness, or unusual discomfort."
        )
        self.progress["value"] = 0
        self.countdown_label.configure(text="Ready for a 3-second contraction")
        self.action.configure(
            text=f"Begin trial {number}",
            state="normal",
            command=lambda: self._countdown(number, 3),
        )

    def _countdown(self, trial_number: int, count: int):
        self.action.configure(state="disabled")
        if count > 0:
            self.countdown_label.configure(text=f"Contract in {count}…")
            self.after_id = self.after(
                1000, lambda: self._countdown(trial_number, count - 1)
            )
            return
        self.countdown_label.configure(text="CONTRACT NOW")
        if isinstance(self.source, DemoSource):
            self.source.mode = "contract"
        self._begin_capture(MVC_SECONDS, lambda: self._finish_trial(trial_number))

    def _finish_trial(self, number: int):
        if isinstance(self.source, DemoSource):
            self.source.mode = "rest"
        stable = [
            frame[0]
            for stamp, frame in self.capture_values
            if MVC_STABLE_START <= stamp - self.capture_start <= MVC_STABLE_END
        ]
        if not self._enough_samples(stable):
            self.countdown_label.configure(text=f"Trial {number} failed")
            self.action.configure(
                text=f"Retry trial {number}",
                state="normal",
                command=lambda: self._countdown(number, 3),
            )
            return
        self.trials.append(stable)
        if number >= MVC_TRIAL_COUNT:
            self._complete()
        else:
            self._rest_between_trials(number + 1, MVC_REST_SECONDS)

    def _rest_between_trials(self, next_trial: int, seconds: int):
        self.progress["value"] = (MVC_REST_SECONDS - seconds) / MVC_REST_SECONDS * 100
        self.countdown_label.configure(text=f"Recovery rest · {seconds} s")
        self.instruction_label.configure(
            text="Relax fully between maximal efforts. This recovery period helps reduce fatigue-related error."
        )
        if seconds <= 0:
            self._prepare_trial(next_trial)
            return
        self.after_id = self.after(
            1000, lambda: self._rest_between_trials(next_trial, seconds - 1)
        )

    def _complete(self):
        try:
            rate = observed_sample_rate(self.rest_timestamps)
            calibration = create_calibration(
                self.muscle, self.rest_samples, self.trials, rate
            )
        except ValueError as exc:
            messagebox.showerror(
                "Calibration could not be accepted", str(exc), parent=self
            )
            self.trials.clear()
            self._prepare_trial(1)
            return
        self.source.close()
        if not self.demo:
            calibration.save(app_data_dir() / "calibration.json")
        self.step_label.configure(text="CALIBRATION COMPLETE")
        self.title_label.configure(text=f"Quality: {calibration.quality_label}")
        self.instruction_label.configure(
            text=(
                f"MVC reference {calibration.mvc_rms_adc:.1f} ADC RMS · "
                f"observed {calibration.sample_rate_hz:.0f} samples/s · "
                f"trial variation {calibration.trial_cv_percent:.1f}%."
            ),
        )
        self.progress["value"] = 100
        self.countdown_label.configure(
            text="Review or repeat the calibration if the contractions were painful or technically poor."
        )
        self.action.configure(
            text="Use this calibration",
            state="normal",
            command=lambda: self._accept(calibration),
        )

    def _accept(self, calibration):
        self.on_complete(calibration)
        self.cancelled = True
        self.destroy()

    def cancel(self):
        self.cancelled = True
        if self.after_id:
            try:
                self.after_cancel(self.after_id)
            except tk.TclError:
                pass
        self.source.close()
        self.destroy()


class CeleryEMGApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · Rehabilitation Biofeedback")
        self.geometry("1180x760")
        self.minsize(1040, 680)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.data_dir = app_data_dir()
        self.calibration_path = self.data_dir / "calibration.json"
        self.current_calibration: Calibration | None = None
        self.port_lookup: dict[str, str] = {}
        self.source = None
        self.session_running = False
        self.session_start = 0.0
        self.last_valid_data = 0.0
        self.rms_buffer = deque()
        self.waveform = deque(maxlen=500)
        self.displayed_mvc = 0.0
        self.current_rms = 0.0
        self.raw_mvc = 0.0
        self.session_file = None
        self.session_writer = None
        self.session_after = None
        self.record_path: Path | None = None
        self.record_graph_data: dict[str, list[tuple[float, float]]] = {}
        self.record_row_count = 0
        self.record_duration = 0.0

        self.demo_var = tk.BooleanVar(value=False)
        self.port_var = tk.StringVar()
        self.muscle_var = tk.StringVar(value="Biceps")
        self.session_label_var = tk.StringVar()
        self.target_low_var = tk.DoubleVar(value=40)
        self.target_high_var = tk.DoubleVar(value=60)
        self.connection_text = tk.StringVar(
            value="Select a device, then run the signal check"
        )

        self._configure_styles()
        self._build_shell()
        self._load_calibration()
        self.refresh_ports()
        self.show_setup()

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TCombobox", padding=8, fieldbackground="white", background="white"
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#E7EFEC",
            background=PRIMARY,
            bordercolor="#E7EFEC",
            lightcolor=PRIMARY,
            darkcolor=PRIMARY,
        )

    def _build_shell(self):
        self.sidebar = tk.Frame(self, bg=NAV, width=235)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=NAV)
        brand.pack(fill="x", padx=24, pady=(27, 36))
        logo = tk.Canvas(brand, width=40, height=40, bg=NAV, highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        logo.create_oval(4, 4, 36, 36, fill=TEAL, outline="")
        logo.create_arc(
            10, 9, 31, 31, start=205, extent=150, style="arc", width=4, outline="white"
        )
        logo.create_line(20, 29, 25, 12, fill="white", width=3)
        tk.Label(
            brand,
            text="CELERY\nEMG",
            bg=NAV,
            fg="white",
            justify="left",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        self.setup_nav = self._nav_button("Setup & calibration", self.show_setup)
        self.setup_nav.pack(fill="x", padx=14, pady=3)
        self.session_nav = self._nav_button("Live session", self.show_session)
        self.session_nav.pack(fill="x", padx=14, pady=3)
        self.records_nav = self._nav_button("Records & graphs", self.show_records)
        self.records_nav.pack(fill="x", padx=14, pady=3)
        tk.Label(
            self.sidebar,
            text="CLINICAL SUPPORT TOOL\nNot a diagnostic device",
            bg=NAV,
            fg="#9EC4B9",
            justify="left",
            font=("Segoe UI", 9),
        ).pack(side="bottom", anchor="w", padx=24, pady=26)

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

    def _nav_button(self, text, command):
        return tk.Button(
            self.sidebar,
            text=text,
            command=command,
            anchor="w",
            bg=NAV,
            fg="#D6E8E2",
            activebackground="#1B4B3F",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=12,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )

    def _clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def _page_header(self, eyebrow: str, title: str, subtitle: str):
        frame = tk.Frame(self.content, bg=BG)
        frame.pack(fill="x", padx=42, pady=(31, 20))
        tk.Label(
            frame, text=eyebrow.upper(), bg=BG, fg=PRIMARY, font=("Segoe UI", 9, "bold")
        ).pack(anchor="w")
        tk.Label(frame, text=title, bg=BG, fg=INK, font=("Segoe UI", 27, "bold")).pack(
            anchor="w", pady=(3, 2)
        )
        tk.Label(frame, text=subtitle, bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(
            anchor="w"
        )

    def _card(self, parent):
        return tk.Frame(
            parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER
        )

    def show_setup(self):
        if self.session_running:
            if not messagebox.askyesno(
                "Stop the active session?",
                "Returning to setup will stop and save the current session.",
            ):
                return
            self.stop_session()
        self._clear_content()
        self._page_header(
            "Clinical workspace",
            "Prepare a biofeedback session",
            "Check the signal, calibrate the selected muscle, then begin monitoring.",
        )

        grid = tk.Frame(self.content, bg=BG)
        grid.pack(fill="both", expand=True, padx=42, pady=(0, 30))
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        grid.rowconfigure(0, weight=1)

        left = self._card(grid)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = tk.Frame(grid, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        self._section_title(left, "1", "Connect the electrode unit")
        port_row = tk.Frame(left, bg=SURFACE)
        port_row.pack(fill="x", padx=26, pady=(3, 10))
        self.port_combo = ttk.Combobox(
            port_row, textvariable=self.port_var, state="readonly", width=45
        )
        self.port_combo.pack(side="left", fill="x", expand=True)
        tk.Button(
            port_row,
            text="Refresh",
            command=self.refresh_ports,
            bg="#EAF1EF",
            fg=INK,
            relief="flat",
            padx=13,
            pady=8,
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        tk.Checkbutton(
            left,
            text="Training / demo mode (no hardware)",
            variable=self.demo_var,
            command=self._demo_changed,
            bg=SURFACE,
            fg=MUTED,
            activebackground=SURFACE,
            selectcolor=SURFACE,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=26)
        test_row = tk.Frame(left, bg=SURFACE)
        test_row.pack(fill="x", padx=26, pady=(13, 25))
        self.test_button = tk.Button(
            test_row,
            text="Run 2-second signal check",
            command=self.test_device,
            bg=PRIMARY,
            fg="white",
            activebackground=PRIMARY_DARK,
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=9,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.test_button.pack(side="left")
        tk.Label(
            test_row,
            textvariable=self.connection_text,
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9),
            wraplength=320,
            justify="left",
        ).pack(side="left", padx=14)

        tk.Frame(left, bg=BORDER, height=1).pack(fill="x", padx=26)
        self._section_title(left, "2", "Configure the session")
        fields = tk.Frame(left, bg=SURFACE)
        fields.pack(fill="x", padx=26, pady=(0, 22))
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        self._field_label(fields, "TARGET MUSCLE", 0, 0)
        muscle = ttk.Combobox(
            fields,
            values=list(MUSCLE_PROFILES),
            textvariable=self.muscle_var,
            state="readonly",
        )
        muscle.grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(4, 13))
        muscle.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_calibration_card()
        )
        self._field_label(fields, "OPTIONAL SESSION LABEL", 0, 1)
        tk.Entry(
            fields,
            textvariable=self.session_label_var,
            bg="#F8FAF9",
            fg=INK,
            relief="solid",
            bd=1,
            font=("Segoe UI", 10),
        ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 13), ipady=8)
        self._field_label(fields, "TARGET %MVC", 2, 0)
        target_frame = tk.Frame(fields, bg=SURFACE)
        target_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Spinbox(
            target_frame,
            from_=0,
            to=149,
            increment=5,
            textvariable=self.target_low_var,
            width=7,
            font=("Segoe UI", 10),
        ).pack(side="left", ipady=5)
        tk.Label(target_frame, text="to", bg=SURFACE, fg=MUTED).pack(
            side="left", padx=10
        )
        tk.Spinbox(
            target_frame,
            from_=1,
            to=150,
            increment=5,
            textvariable=self.target_high_var,
            width=7,
            font=("Segoe UI", 10),
        ).pack(side="left", ipady=5)
        tk.Label(
            target_frame, text="% MVC", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)
        ).pack(side="left", padx=10)

        calibration_card = self._card(right)
        calibration_card.pack(fill="x", pady=(0, 20))
        self._section_title(calibration_card, "3", "MVC calibration")
        self.calibration_status = tk.Label(
            calibration_card,
            bg=SURFACE,
            fg=INK,
            font=("Segoe UI", 11, "bold"),
            justify="left",
        )
        self.calibration_status.pack(anchor="w", padx=26)
        self.calibration_detail = tk.Label(
            calibration_card,
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9),
            justify="left",
            wraplength=300,
        )
        self.calibration_detail.pack(anchor="w", padx=26, pady=(6, 16))
        tk.Button(
            calibration_card,
            text="Run guided calibration",
            command=self.open_calibration,
            bg="#E6F3EF",
            fg=PRIMARY_DARK,
            relief="flat",
            padx=16,
            pady=10,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(anchor="w", padx=26, pady=(0, 25))

        action_card = tk.Frame(right, bg=NAV)
        action_card.pack(fill="both", expand=True)
        tk.Label(
            action_card,
            text="READY WHEN YOU ARE",
            bg=NAV,
            fg="#9FD5C4",
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=26, pady=(27, 7))
        tk.Label(
            action_card,
            text="Live muscle\nactivation feedback",
            bg=NAV,
            fg="white",
            justify="left",
            font=("Segoe UI", 20, "bold"),
        ).pack(anchor="w", padx=26)
        tk.Label(
            action_card,
            text="The session is recorded locally as CSV for later review.",
            bg=NAV,
            fg="#BED7D0",
            justify="left",
            wraplength=285,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=26, pady=(10, 20))
        tk.Button(
            action_card,
            text="Start live session  →",
            command=self.show_session,
            bg=TEAL,
            fg="white",
            activebackground=PRIMARY,
            activeforeground="white",
            relief="flat",
            padx=19,
            pady=12,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        ).pack(anchor="w", padx=26, pady=(0, 28))
        self._update_calibration_card()

    def _section_title(self, parent, number, title):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", padx=26, pady=(24, 16))
        tk.Label(
            row,
            text=number,
            bg=MINT,
            fg=PRIMARY,
            width=3,
            height=1,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            row, text=title, bg=SURFACE, fg=INK, font=("Segoe UI", 13, "bold")
        ).pack(side="left", padx=10)

    def _field_label(self, parent, text, row, column):
        tk.Label(
            parent, text=text, bg=SURFACE, fg=MUTED, font=("Segoe UI", 8, "bold")
        ).grid(row=row, column=column, sticky="w", padx=(0 if column == 0 else 8, 0))

    def _load_calibration(self):
        candidates = [
            self.calibration_path,
            Path(__file__).resolve().with_name("mvc_calibration.json"),
        ]
        for path in candidates:
            if not path.exists():
                continue
            try:
                self.current_calibration = Calibration.load(path)
                return
            except (ValueError, OSError, KeyError, TypeError):
                continue

    def _update_calibration_card(self):
        if not hasattr(self, "calibration_status"):
            return
        cal = self.current_calibration
        if self.demo_var.get():
            self.calibration_status.configure(
                text="Demo calibration available", fg=PRIMARY
            )
            self.calibration_detail.configure(
                text="Synthetic data only · no patient measurement"
            )
        elif cal and cal.muscle == self.muscle_var.get():
            self.calibration_status.configure(
                text=f"{cal.quality_label} · {cal.muscle}", fg=PRIMARY
            )
            self.calibration_detail.configure(
                text=f"MVC {cal.mvc_rms_adc:.1f} ADC RMS · {cal.sample_rate_hz:.0f} samples/s\n"
                f"Trial variation {cal.trial_cv_percent:.1f}% · {cal.created_at[:10]}"
            )
        elif cal:
            self.calibration_status.configure(text="Calibration required", fg=AMBER)
            self.calibration_detail.configure(
                text=f"The saved calibration is for {cal.muscle}, not {self.muscle_var.get()}."
            )
        else:
            self.calibration_status.configure(text="No calibration saved", fg=AMBER)
            self.calibration_detail.configure(
                text="Run a guided baseline and three MVC trials."
            )

    def refresh_ports(self):
        self.port_lookup.clear()
        displays = []
        if list_ports is not None:
            for item in list_ports.comports():
                display = f"{item.device}  ·  {item.description}"
                displays.append(display)
                self.port_lookup[display] = item.device
        if hasattr(self, "port_combo"):
            self.port_combo["values"] = displays
        if displays:
            current = self.port_var.get()
            if current not in displays:
                self.port_var.set(displays[0])
        else:
            self.port_var.set("")
            self.connection_text.set(
                "No serial device found · connect the STM32 or use demo mode"
            )

    def selected_port(self) -> str:
        return self.port_lookup.get(
            self.port_var.get(), self.port_var.get().split(" ")[0]
        )

    def _demo_changed(self):
        if self.demo_var.get():
            self.connection_text.set(
                "Demo source selected · hardware is not being measured"
            )
        else:
            self.connection_text.set("Run the signal check before calibration")
            self._load_calibration()
        self._update_calibration_card()

    def _validate_setup(self, require_calibration=True) -> bool:
        if not self.demo_var.get() and not self.selected_port():
            messagebox.showwarning(
                "Select a device", "Connect the STM32 and select its COM port first."
            )
            return False
        try:
            low = float(self.target_low_var.get())
            high = float(self.target_high_var.get())
        except (ValueError, tk.TclError):
            messagebox.showwarning("Invalid target", "Enter numerical target values.")
            return False
        if low < 0 or high > 150 or high <= low:
            messagebox.showwarning(
                "Invalid target",
                "Use a target from 0–150% with the upper value above the lower value.",
            )
            return False
        if (
            require_calibration
            and not self.demo_var.get()
            and (
                not self.current_calibration
                or self.current_calibration.muscle != self.muscle_var.get()
            )
        ):
            messagebox.showwarning(
                "Calibration required",
                "Run MVC calibration for the selected muscle before starting.",
            )
            return False
        return True

    def test_device(self):
        if not self._validate_setup(require_calibration=False):
            return
        self.test_button.configure(state="disabled", text="Checking signal…")
        self.connection_text.set("Collecting valid ADC samples for 2 seconds…")
        port, demo = self.selected_port(), self.demo_var.get()

        def worker():
            source = make_source(port, demo)
            frames, stamps = [], []
            error = None
            try:
                source.start()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    for stamp, frame in source.drain():
                        stamps.append(stamp)
                        frames.append(frame)
                    time.sleep(0.02)
                for stamp, frame in source.drain():
                    stamps.append(stamp)
                    frames.append(frame)
                error = source.error
            except (OSError, ValueError, RuntimeError, serial.SerialException) as exc:
                error = str(exc)
            finally:
                source.close()
            self.after(0, lambda: self._finish_device_test(frames, stamps, error))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_device_test(self, frames, stamps, error):
        if not self.winfo_exists():
            return
        self.test_button.configure(state="normal", text="Run 2-second signal check")
        if error:
            self.connection_text.set("Signal check failed")
            messagebox.showerror("Device check failed", error)
            return
        if len(frames) < 50:
            self.connection_text.set(f"Failed · only {len(frames)} valid samples")
            messagebox.showerror(
                "No usable data",
                "The port opened, but too few valid ADC samples arrived. Check firmware and baud rate.",
            )
            return
        if any(len(frame) != len(CHANNELS) for frame in frames):
            self.connection_text.set("Firmware update required · only A0 received")
            messagebox.showerror(
                "Six-channel firmware required",
                "The device is using the older A0-only stream. Flash the updated STM32 firmware before recording a session.",
            )
            return
        values = [frame[0] for frame in frames]
        rate = observed_sample_rate(stamps)
        signal_range = max(values) - min(values)
        if rate < 100:
            self.connection_text.set(f"Failed · sample rate too low ({rate:.0f}/s)")
            messagebox.showerror(
                "Sample rate too low",
                "The device is sending fewer than 100 valid samples per second. Check the firmware timer and serial connection.",
            )
            return
        if signal_range < 2:
            self.connection_text.set("Failed · signal is flat")
            messagebox.showerror(
                "Flat signal",
                "The ADC signal is not changing. Check electrode contact, analogue power, and the ADC input.",
            )
            return
        clipped = sum(value in (0, ADC_MAX) for value in values) / len(values) * 100
        self.connection_text.set(
            f"6 channels OK · {rate:.0f} frames/s · A0 range {min(values)}–{max(values)} ADC"
        )
        if clipped > 5.0:
            self.connection_text.set(
                f"Review signal · {clipped:.1f}% at an ADC rail · {rate:.0f} samples/s"
            )
            messagebox.showwarning(
                "Signal clipping detected",
                "More than 5% of samples are at an ADC rail. Check electrode contact, gain, and analogue power before calibration.",
            )

    def open_calibration(self):
        if not self._validate_setup(require_calibration=False):
            return
        CalibrationDialog(
            self,
            self.selected_port(),
            self.muscle_var.get(),
            self.demo_var.get(),
            self._calibration_complete,
        )

    def _calibration_complete(self, calibration: Calibration):
        self.current_calibration = calibration
        self._update_calibration_card()

    def _demo_calibration(self) -> Calibration:
        return Calibration(
            muscle=self.muscle_var.get(),
            baseline_adc=100.0,
            rest_rms_adc=3.0,
            mvc_rms_adc=420.0,
            sample_rate_hz=1000.0,
            mvc_trials=[412.0, 425.0, 423.0],
            trial_cv_percent=1.4,
        )

    def show_records(self):
        if self.session_running:
            if not messagebox.askyesno(
                "Stop the active session?",
                "Opening records will stop and save the current session.",
            ):
                return
            self.stop_session()
        self._clear_content()
        self._page_header(
            "Session records",
            "CSV review & channel graphs",
            "Open any Celery EMG session and compare all six recorded analogue signals.",
        )

        toolbar = self._card(self.content)
        toolbar.pack(fill="x", padx=42, pady=(0, 14))
        tk.Button(
            toolbar,
            text="Open CSV…",
            command=self.choose_csv,
            bg=PRIMARY,
            fg="white",
            activebackground=PRIMARY_DARK,
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(18, 8), pady=15)
        self.spreadsheet_button = tk.Button(
            toolbar,
            text="Open in spreadsheet",
            command=self.open_csv_external,
            bg="#EAF1EF",
            fg=INK,
            relief="flat",
            padx=16,
            pady=10,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            state="disabled",
        )
        self.spreadsheet_button.pack(side="left", pady=15)
        self.record_file_label = tk.Label(
            toolbar,
            text="No CSV selected",
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="e",
        )
        self.record_file_label.pack(side="right", fill="x", expand=True, padx=20)

        graph_card = self._card(self.content)
        graph_card.pack(fill="both", expand=True, padx=42, pady=(0, 16))
        self.record_canvas = tk.Canvas(
            graph_card, bg=SURFACE, highlightthickness=0, height=500
        )
        self.record_canvas.pack(fill="both", expand=True, padx=18, pady=(18, 5))
        self.record_canvas.bind(
            "<Configure>", lambda _event: self._draw_record_graphs()
        )
        self.record_summary = tk.Label(
            graph_card,
            text="Choose a CSV to display A0, A1, A2, A3, A4, and A6.",
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.record_summary.pack(anchor="w", padx=22, pady=(5, 16))

        sessions = self.data_dir / "sessions"
        existing = list(sessions.glob("*.csv")) if sessions.exists() else []
        if existing:
            self.load_csv(max(existing, key=lambda item: item.stat().st_mtime))
        else:
            self._draw_record_graphs()

    def choose_csv(self):
        sessions = self.data_dir / "sessions"
        path = filedialog.askopenfilename(
            parent=self,
            title="Open Celery EMG session",
            initialdir=sessions if sessions.exists() else self.data_dir,
            filetypes=(("CSV session files", "*.csv"), ("All files", "*.*")),
        )
        if path:
            self.load_csv(Path(path))

    def load_csv(self, path: Path):
        data = {channel: [] for channel in CHANNELS}
        stride = 1
        max_points = 3500
        row_count = 0
        last_elapsed = 0.0
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                fields = set(reader.fieldnames or [])
                has_new_a0 = CHANNEL_CSV_COLUMNS["A0"] in fields
                has_legacy_a0 = "raw_adc" in fields
                if not has_new_a0 and not has_legacy_a0:
                    raise ValueError(
                        "This CSV does not contain a recognised A0 signal column."
                    )
                for row_count, row in enumerate(reader, start=1):
                    try:
                        elapsed = float(row.get("elapsed_seconds") or row_count - 1)
                    except ValueError:
                        continue
                    last_elapsed = elapsed
                    if (row_count - 1) % stride != 0:
                        continue
                    for channel in CHANNELS:
                        column = CHANNEL_CSV_COLUMNS[channel]
                        raw = row.get(column)
                        if channel == "A0" and raw in (None, ""):
                            raw = row.get("raw_adc")
                        if raw not in (None, ""):
                            try:
                                data[channel].append((elapsed, float(raw)))
                            except ValueError:
                                pass
                    if len(data["A0"]) > max_points * 2:
                        for channel in CHANNELS:
                            data[channel] = data[channel][::2]
                        stride *= 2
        except (OSError, csv.Error, ValueError) as exc:
            messagebox.showerror("Could not open CSV", str(exc), parent=self)
            return

        if not data["A0"]:
            messagebox.showerror(
                "No graphable data",
                "No numerical A0 samples were found in this CSV.",
                parent=self,
            )
            return
        self.record_path = path
        self.record_graph_data = data
        self.record_row_count = row_count
        self.record_duration = last_elapsed
        self.record_file_label.configure(text=path.name)
        self.spreadsheet_button.configure(state="normal")
        rate = row_count / last_elapsed if last_elapsed > 0 else 0.0
        available = sum(bool(data[channel]) for channel in CHANNELS)
        self.record_summary.configure(
            text=(
                f"{row_count:,} frames · {last_elapsed:.1f} seconds · approximately {rate:.0f} frames/s · "
                f"{available} of {len(CHANNELS)} channels available"
            )
        )
        self._draw_record_graphs()

    def open_csv_external(self):
        if not self.record_path:
            return
        try:
            os.startfile(self.record_path)
        except OSError as exc:
            messagebox.showerror("Could not open spreadsheet", str(exc), parent=self)

    def _draw_record_graphs(self):
        if not hasattr(self, "record_canvas") or not self.record_canvas.winfo_exists():
            return
        canvas = self.record_canvas
        canvas.delete("all")
        width = max(100, canvas.winfo_width())
        height = max(180, canvas.winfo_height())
        if not self.record_graph_data:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Open a session CSV to view all recorded channels",
                fill=MUTED,
                font=("Segoe UI", 12),
            )
            return

        colors = ("#13795B", "#2878B5", "#8B5FBF", "#D17A22", "#D14D72", "#4E7B35")
        left, right = 175, width - 62
        row_height = height / len(CHANNELS)
        duration = max(self.record_duration, 0.001)
        for index, channel in enumerate(CHANNELS):
            top = index * row_height
            bottom = (index + 1) * row_height
            middle = (top + bottom) / 2
            canvas.create_rectangle(
                0,
                top,
                width,
                bottom,
                fill="#FBFCFC" if index % 2 == 0 else "#F5F8F7",
                outline="",
            )
            canvas.create_line(left, bottom - 5, right, bottom - 5, fill="#DCE5E2")
            canvas.create_text(
                14,
                middle - 8,
                text=f"{channel}  {CHANNEL_LABELS[channel]}",
                anchor="w",
                fill=INK,
                font=("Segoe UI", 9, "bold"),
            )
            points = self.record_graph_data.get(channel, [])
            if not points:
                canvas.create_text(
                    left + 8,
                    middle,
                    text="Not present in this CSV",
                    anchor="w",
                    fill=MUTED,
                    font=("Segoe UI", 9),
                )
                continue
            low = min(value for _, value in points)
            high = max(value for _, value in points)
            span = max(high - low, 1.0)
            plot_top, plot_bottom = top + 9, bottom - 9
            coords = []
            for elapsed, value in points:
                x = left + elapsed / duration * (right - left)
                y = plot_bottom - (value - low) / span * (plot_bottom - plot_top)
                coords.extend((x, y))
            if len(coords) >= 4:
                canvas.create_line(*coords, fill=colors[index], width=1.5)
            canvas.create_text(
                width - 10,
                top + 12,
                text=f"{high:.0f}",
                anchor="e",
                fill=MUTED,
                font=("Segoe UI", 8),
            )
            canvas.create_text(
                width - 10,
                bottom - 12,
                text=f"{low:.0f}",
                anchor="e",
                fill=MUTED,
                font=("Segoe UI", 8),
            )
        canvas.create_text(
            left,
            height - 2,
            text="0 s",
            anchor="sw",
            fill=MUTED,
            font=("Segoe UI", 8),
        )
        canvas.create_text(
            right,
            height - 2,
            text=f"{self.record_duration:.1f} s",
            anchor="se",
            fill=MUTED,
            font=("Segoe UI", 8),
        )

    def show_session(self):
        if self.session_running:
            return
        if not self._validate_setup(require_calibration=True):
            return
        if self.demo_var.get():
            self.current_calibration = self._demo_calibration()
        self._clear_content()
        self._page_header(
            "Live biofeedback",
            self.muscle_var.get(),
            "Use the target cue alongside clinical observation and patient feedback.",
        )

        toolbar = tk.Frame(self.content, bg=BG)
        toolbar.pack(fill="x", padx=42, pady=(0, 15))
        self.live_dot = tk.Label(
            toolbar, text="●  READY", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")
        )
        self.live_dot.pack(side="left")
        self.timer_label = tk.Label(
            toolbar, text="00:00", bg=BG, fg=INK, font=("Segoe UI", 13, "bold")
        )
        self.timer_label.pack(side="left", padx=22)
        self.stop_button = tk.Button(
            toolbar,
            text="Stop & save",
            command=self.stop_session,
            bg="#F5E8E8",
            fg=RED,
            relief="flat",
            padx=16,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            state="disabled",
        )
        self.stop_button.pack(side="right")

        body = tk.Frame(self.content, bg=BG)
        body.pack(fill="both", expand=True, padx=42, pady=(0, 32))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        left = self._card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = self._card(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        tk.Label(
            left,
            text="MUSCLE ACTIVATION",
            bg=SURFACE,
            fg=MUTED,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=30, pady=(28, 0))
        self.mvc_label = tk.Label(
            left, text="0.0%", bg=SURFACE, fg=INK, font=("Segoe UI", 55, "bold")
        )
        self.mvc_label.pack(anchor="w", padx=27, pady=(0, 0))
        tk.Label(
            left, text="of calibrated MVC", bg=SURFACE, fg=MUTED, font=("Segoe UI", 10)
        ).pack(anchor="w", padx=31)
        self.gauge = tk.Canvas(left, height=62, bg=SURFACE, highlightthickness=0)
        self.gauge.pack(fill="x", padx=30, pady=(20, 10))
        self.gauge.bind("<Configure>", lambda _event: self._draw_gauge())
        self.wave_canvas = tk.Canvas(
            left, height=210, bg="#F7FAF9", highlightthickness=0
        )
        self.wave_canvas.pack(fill="both", expand=True, padx=30, pady=(10, 28))
        self.wave_canvas.bind("<Configure>", lambda _event: self._draw_waveform())

        self.cue_frame = tk.Frame(right, bg=MINT)
        self.cue_frame.pack(fill="x", padx=24, pady=24)
        tk.Label(
            self.cue_frame,
            text="THERAPY CUE",
            bg=MINT,
            fg=PRIMARY,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=20, pady=(18, 4))
        self.cue_label = tk.Label(
            self.cue_frame,
            text="Ready to begin",
            bg=MINT,
            fg=PRIMARY_DARK,
            font=("Segoe UI", 18, "bold"),
            wraplength=280,
            justify="left",
        )
        self.cue_label.pack(anchor="w", padx=20, pady=(0, 18))
        self._metric(
            right,
            "TARGET RANGE",
            f"{self.target_low_var.get():.0f}–{self.target_high_var.get():.0f}% MVC",
        )
        self.rms_metric = self._metric(right, "CURRENT RMS", "0.0 ADC RMS")
        cal = self.current_calibration
        self._metric(right, "MVC REFERENCE", f"{cal.mvc_rms_adc:.1f} ADC RMS")
        self.data_metric = self._metric(right, "SIGNAL", "Waiting for data")
        tk.Label(
            right,
            text="Stop immediately if the patient reports pain, dizziness, numbness, or unusual discomfort.",
            bg=SURFACE,
            fg=MUTED,
            wraplength=300,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(side="bottom", anchor="w", padx=24, pady=22)

        self.start_button = tk.Button(
            right,
            text="Begin recording",
            command=self.start_session,
            bg=PRIMARY,
            fg="white",
            activebackground=PRIMARY_DARK,
            activeforeground="white",
            relief="flat",
            padx=20,
            pady=12,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )
        self.start_button.pack(side="bottom", fill="x", padx=24, pady=(0, 8))

    def _metric(self, parent, label, value):
        frame = tk.Frame(parent, bg=SURFACE)
        frame.pack(fill="x", padx=24, pady=7)
        tk.Label(
            frame, text=label, bg=SURFACE, fg=MUTED, font=("Segoe UI", 8, "bold")
        ).pack(anchor="w")
        output = tk.Label(
            frame, text=value, bg=SURFACE, fg=INK, font=("Segoe UI", 11, "bold")
        )
        output.pack(anchor="w", pady=(2, 0))
        return output

    def start_session(self):
        self.source = make_source(self.selected_port(), self.demo_var.get())
        try:
            self.source.start()
        except (OSError, ValueError, RuntimeError, serial.SerialException) as exc:
            messagebox.showerror("Could not start session", str(exc))
            return
        cal = self.current_calibration
        window_size = max(10, round(cal.sample_rate_hz * cal.rms_window_seconds))
        self.rms_buffer = deque(maxlen=window_size)
        self.waveform.clear()
        self.displayed_mvc = self.current_rms = self.raw_mvc = 0.0
        self.session_start = time.monotonic()
        self.last_valid_data = self.session_start
        self._open_session_log()
        self.session_running = True
        self.start_button.configure(state="disabled", text="Recording in progress")
        self.stop_button.configure(state="normal")
        self.live_dot.configure(text="●  LIVE", fg=TEAL)
        self._session_tick()

    def _open_session_log(self):
        sessions = self.data_dir / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        label = re.sub(
            r"[^A-Za-z0-9_-]+", "-", self.session_label_var.get().strip()
        ).strip("-")
        suffix = f"_{label}" if label else ""
        filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + suffix + ".csv"
        self.session_file = (sessions / filename).open(
            "w", newline="", encoding="utf-8"
        )
        self.session_writer = csv.writer(self.session_file)
        self.session_writer.writerow(
            [
                "local_time",
                "elapsed_seconds",
                "muscle",
                *[CHANNEL_CSV_COLUMNS[channel] for channel in CHANNELS],
                "a0_corrected_adc",
                "a0_rms_adc",
                "percent_mvc",
                "target_low",
                "target_high",
                "demo_mode",
            ]
        )

    def _session_tick(self):
        if not self.session_running:
            return
        batch = self.source.drain()
        cal = self.current_calibration
        now = time.monotonic()
        for stamp, frame in batch:
            if len(frame) != len(CHANNELS):
                self.source.error = (
                    "Legacy A0-only firmware detected. Flash the six-channel firmware."
                )
                continue
            a0 = frame[0]
            corrected = max(0.0, a0 - cal.baseline_adc)
            self.rms_buffer.append(corrected)
            self.waveform.append(corrected)
            self.last_valid_data = stamp
            if len(self.rms_buffer) == self.rms_buffer.maxlen:
                self.current_rms = rms(self.rms_buffer)
                self.raw_mvc = percent_mvc(self.current_rms, cal.mvc_rms_adc)
            self.session_writer.writerow(
                [
                    datetime.now().isoformat(timespec="milliseconds"),
                    f"{stamp - self.session_start:.3f}",
                    cal.muscle,
                    *frame,
                    f"{corrected:.3f}",
                    f"{self.current_rms:.3f}",
                    f"{self.raw_mvc:.3f}",
                    self.target_low_var.get(),
                    self.target_high_var.get(),
                    self.demo_var.get(),
                ]
            )
        if batch:
            self.session_file.flush()
        self.displayed_mvc = 0.20 * self.raw_mvc + 0.80 * self.displayed_mvc
        elapsed = now - self.session_start
        self.timer_label.configure(
            text=f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
        )
        self.mvc_label.configure(text=f"{self.displayed_mvc:.1f}%")
        self.rms_metric.configure(text=f"{self.current_rms:.1f} ADC RMS")
        connected = now - self.last_valid_data <= 1.5 and not self.source.error
        self.data_metric.configure(
            text="Receiving valid ADC data"
            if connected
            else "No recent data · check device",
            fg=PRIMARY if connected else RED,
        )
        self._update_cue(connected)
        self._draw_gauge()
        self._draw_waveform()
        if self.source.error:
            self.live_dot.configure(text="●  DEVICE ERROR", fg=RED)
        self.session_after = self.after(33, self._session_tick)

    def _update_cue(self, connected):
        if not connected:
            text, color, panel = "Check signal", RED, "#F8E8E8"
        elif self.displayed_mvc < self.target_low_var.get():
            text, color, panel = "Increase effort", AMBER, "#FFF3DD"
        elif self.displayed_mvc <= self.target_high_var.get():
            text, color, panel = "In target range", PRIMARY, MINT
        else:
            text, color, panel = "Reduce effort", RED, "#F8E8E8"
        self.cue_frame.configure(bg=panel)
        for child in self.cue_frame.winfo_children():
            child.configure(bg=panel)
        self.cue_label.configure(text=text, fg=color)

    def _draw_gauge(self):
        if not hasattr(self, "gauge") or not self.gauge.winfo_exists():
            return
        canvas = self.gauge
        canvas.delete("all")
        width = max(10, canvas.winfo_width())
        left, right, y1, y2 = 2, width - 2, 14, 38
        canvas.create_rectangle(left, y1, right, y2, fill="#E6EEEB", outline="")
        scale_max = 150.0
        low_x = (
            left
            + (right - left) * min(self.target_low_var.get(), scale_max) / scale_max
        )
        high_x = (
            left
            + (right - left) * min(self.target_high_var.get(), scale_max) / scale_max
        )
        canvas.create_rectangle(low_x, y1, high_x, y2, fill="#A9DEC9", outline="")
        value_x = left + (right - left) * min(self.displayed_mvc, scale_max) / scale_max
        canvas.create_rectangle(left, y1, value_x, y2, fill=PRIMARY, outline="")
        canvas.create_text(
            left, 51, text="0", anchor="w", fill=MUTED, font=("Segoe UI", 8)
        )
        canvas.create_text(
            right, 51, text="150%", anchor="e", fill=MUTED, font=("Segoe UI", 8)
        )

    def _draw_waveform(self):
        if not hasattr(self, "wave_canvas") or not self.wave_canvas.winfo_exists():
            return
        canvas = self.wave_canvas
        canvas.delete("all")
        width, height = max(10, canvas.winfo_width()), max(10, canvas.winfo_height())
        for fraction in (0.25, 0.5, 0.75):
            y = height * fraction
            canvas.create_line(0, y, width, y, fill="#E3EBE8")
        values = list(self.waveform)
        if len(values) < 2:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Signal waveform will appear here",
                fill=MUTED,
                font=("Segoe UI", 10),
            )
            return
        scale = max(self.current_calibration.mvc_rms_adc * 1.6, max(values), 1.0)
        step = width / (len(values) - 1)
        points = []
        for index, value in enumerate(values):
            points.extend(
                (index * step, height - 8 - min(1.0, value / scale) * (height - 16))
            )
        canvas.create_line(*points, fill=TEAL, width=2, smooth=True)

    def stop_session(self, notify=True):
        if not self.session_running:
            return
        self.session_running = False
        if self.session_after:
            try:
                self.after_cancel(self.session_after)
            except tk.TclError:
                pass
        if self.source:
            self.source.close()
        saved_path = Path(self.session_file.name) if self.session_file else None
        if self.session_file:
            self.session_file.flush()
            self.session_file.close()
        self.session_file = self.session_writer = None
        if hasattr(self, "live_dot") and self.live_dot.winfo_exists():
            self.live_dot.configure(text="●  SAVED", fg=PRIMARY)
            self.stop_button.configure(state="disabled")
            self.start_button.configure(state="normal", text="Begin another recording")
        if notify and saved_path:
            messagebox.showinfo(
                "Session saved", f"The session was saved locally to:\n{saved_path}"
            )

    def close_app(self):
        if self.session_running:
            self.stop_session(notify=False)
        self.destroy()


def main():
    if serial is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing dependency",
            "pyserial is not installed. Run Launch Celery EMG.bat, which installs the required component automatically.",
        )
        root.destroy()
        return
    app = CeleryEMGApp()
    app.mainloop()


if __name__ == "__main__":
    main()
