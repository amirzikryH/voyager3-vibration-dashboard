# Voyager3 Vibration Dashboard

A local web dashboard for real-time vibration monitoring with the Analog Devices **EV-CBM-VOYAGER3-1Z** wireless sensor kit.

## Features

- **Auto connect:** Detects the SmartMesh Manager COM port and finds the sensor automatically.
- **Live monitoring:** X, Y and Z time signals, FFT and RMS values in real time.
- **Sampling settings:** Change frequency, sample count, axes and sleep time from the dashboard.
- **Data recording:** Save data as CSV, XLSX or TXT with your own filename.
- **Fault prediction:** Plug in any model through `app/predictor.py`.
- **Demo mode:** Try the dashboard with synthetic data and no hardware.

## Requirements

- Python 3.10+
- EV-CBM-VOYAGER3-1Z kit (SmartMesh Manager dongle and Voyager3 sensor) for live mode

```bash
pip install -r requirements.txt
```

## How to Run

**Demo mode (no hardware):**

```bash
python app/server.py --demo
```

**Live mode (dongle plugged in):**

```bash
python app/server.py
```

On Windows you can also double-click `run_demo.bat` or `run.bat`.

The dashboard opens at http://127.0.0.1:8000. Click **Connect** to start.

## Configuration

Edit `config.json` or use the dashboard settings panel.

| Key | Description |
|-----|-------------|
| `sampling_frequency` | Sensor sampling rate (Hz) |
| `adc_num_samples` | Samples per segment |
| `axis_select` | Enabled axes, e.g. `"111"` for X, Y and Z |
| `sleep_dur` | Sleep time between segments |
| `com_port` | Set a port like `"COM7"`, or `null` to auto-detect |
| `model_dir` | Folder with your model files |
| `healthy_label` | Class name that means no fault |

## Using Your Own Model

The included predictor is a simple RMS threshold example so the dashboard works out of the box.
To use your own model, edit `app/predictor.py`:

1. Load your model in `__init__`.
2. Run inference in `predict(wave)` and return `label`, `confidence` and `probs`.

`wave` is the latest segment in g: `{"x": [...], "y": [...], "z": [...]}`.

## Project Structure

```
├── app/
│   ├── server.py          # Flask server and API
│   ├── acquisition.py     # Sensor connection and data parsing
│   ├── predictor.py       # Swappable fault prediction model
│   └── templates/
│       └── index.html     # Dashboard page
├── SmartMeshSDK/          # Analog Devices SmartMesh SDK (ported to Python 3)
├── config.json
├── requirements.txt
└── run.bat / run.sh / run_demo.bat / run_demo.sh
```

## Acknowledgements

`SmartMeshSDK/` is the Analog Devices (Dust Networks) SmartMesh SDK, ported from Python 2.7 to Python 3.10+ for this project.
