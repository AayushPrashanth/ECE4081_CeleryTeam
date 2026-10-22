# Celery EMG rehabilitation biofeedback

Celery EMG is a Windows desktop interface for the ECE4081 STM32 EMG prototype. It combines the device check, guided MVC calibration, live target feedback, six-channel CSV recording, and session graph review in one clinician-facing workflow.

> **Prototype scope:** this software displays relative, within-session muscle activation. It is not a diagnostic or treatment-decision device. A trained clinician remains responsible for electrode placement, contraindication screening, patient observation, target selection, and stopping the activity if symptoms occur.

## Quick start on Windows

1. Flash the STM32 firmware and connect the board by USB.
2. Double-click **`Launch Celery EMG.bat`**.
3. On first launch, the script creates a private Python environment and installs `pyserial`. Later launches open directly.
4. In the app, select the STM32 COM port and run the 2-second signal check.
5. Select the target muscle, run guided calibration, set the target band, and start the live session.
6. Open **Records & graphs** to load any saved CSV and review the six signals. **Open in spreadsheet** launches the selected CSV in the Windows default CSV application.

Use **Training / demo mode** to explore the full workflow without hardware. Demo mode is clearly labelled and does not overwrite the real calibration.

## Standalone EXE

Double-click **`Build Celery EMG EXE.bat`** once. It creates:

```text
dist\CeleryEMG.exe
```

That single file can be launched on another Windows PC without installing Python. Windows may show a SmartScreen warning for an unsigned university prototype; code-sign the final executable before formal distribution.

## Data storage

Calibration and session data are stored per Windows user:

```text
%LOCALAPPDATA%\CeleryEMG\
├── calibration.json
└── sessions\YYYY-MM-DD_HH-MM-SS_optional-label.csv
```

The optional session label becomes part of the filename. Avoid entering identifiable patient information unless your organisation has approved the storage and handling process.

## Device contract

The desktop app expects the updated firmware to send six comma-separated 12-bit ADC values per frame at **460800 baud** in this fixed order:

| CSV signal | Nucleo header | MCU ADC channel | Intended signal |
|---|---|---|---|
| `a0_final_adc` | A0 / PA0 | ADC1_IN5 | Final signal used for MVC feedback |
| `a1_before_notch_adc` | A1 / PA1 | ADC1_IN6 | Before notch filter |
| `a2_before_notch_adc` | A2 / PA3 | ADC1_IN8 | Before notch filter |
| `a3_reference_adc` | A3 / PA4 | ADC1_IN9 | Reference electrode |
| `a4_electrode_1_adc` | A4 / PA5 | ADC1_IN10 | Electrode 1 |
| `a6_electrode_2_adc` | A6 / PA7 | ADC1_IN12 | Electrode 2 |

For example:

```text
103,110,108,1652,1532,1468
98,105,101,1650,1529,1471
```

The device check verifies that all six channels are present and reports observed frames/second, A0 range, and A0 rail clipping. Calibration and `%MVC` use A0. Calibration requires a resting capture and three 3-second MVC efforts, with a 30-second recovery between efforts. Only the stable middle two seconds of each MVC trial are used.

The request mentioned A5 after explicitly listing A6. This implementation records A6 as requested. A5/PA6 is intentionally left unconfigured until its intended signal is confirmed; adding it later requires one additional ADC rank and CSV column.

## Developer checks

From this folder:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_emg_core.py
.\.venv\Scripts\python.exe -m py_compile celery_emg_app.py emg_core.py
```

The older `mvc_*.py` and `emg_test*.py` files are retained as experiment history. `celery_emg_app.py` is the integrated application entry point.
