"""
Voyager3 local dashboard server.

Serves the status dashboard and a small JSON API the page polls once a second.
Run:  python app/server.py           (hardware mode)
      python app/server.py --demo     (synthetic data, no dongle needed)

Then open http://127.0.0.1:8000 in any browser.
"""

import os
import sys
import json
import argparse
import logging
import webbrowser
import threading

from flask import Flask, jsonify, request, render_template

# Silence Flask's per-request access log (the constant "GET /api/status 200" lines).
logging.getLogger("werkzeug").setLevel(logging.ERROR)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.acquisition import AcquisitionService, AXIS_ENCODINGS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.dirname(HERE), "config.json")

DEFAULT_CONFIG = {
    "sampling_frequency": 512,
    "adc_num_samples":    512,
    "axis_select":        "111",
    "sleep_dur":          0,
    "com_port":           None,   # null = auto-detect (highest COM)
    "model_dir":          None,   # folder with your model files (see app/predictor.py)
    "healthy_label":      "Normal",
    "rms_threshold_g":    0.5,    # used by the example predictor only
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
            static_folder=os.path.join(HERE, "static"))

config = load_config()
service = AcquisitionService(config)
DEMO_MODE = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(service.snapshot())


@app.route("/api/connect", methods=["POST"])
def api_connect():
    body = request.get_json(force=True, silent=True) or {}
    # A blank COM field means "use the saved/auto-detected port" -- don't
    # overwrite a known-good saved port with None.
    port = (body.get("com_port") or "").strip()
    if port:
        config["com_port"] = port
        service.config["com_port"] = port
        save_config(config)
    service.start(demo=DEMO_MODE)
    return jsonify({"ok": True})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    service.stop()
    return jsonify({"ok": True})


@app.route("/api/remove_offset", methods=["POST"])
def api_remove_offset():
    service.request_offset_removal()
    return jsonify({"ok": True})


@app.route("/api/predict/start", methods=["POST"])
def api_predict_start():
    service.start_prediction()
    return jsonify({"ok": True})


@app.route("/api/predict/stop", methods=["POST"])
def api_predict_stop():
    service.stop_prediction()
    return jsonify({"ok": True})


@app.route("/api/choose_folder", methods=["POST"])
def api_choose_folder():
    """Open a native OS folder-picker on the machine running this server."""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose a folder to save recordings")
        root.destroy()
        return jsonify({"ok": True, "path": path or ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "path": ""}), 200


@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    body = request.get_json(force=True, silent=True) or {}
    folder = (body.get("folder") or "").strip()
    filename = (body.get("filename") or "").strip()
    fmt = (body.get("format") or "csv").lower()
    if not folder:
        return jsonify({"ok": False, "error": "No folder chosen"}), 400
    if not filename:
        return jsonify({"ok": False, "error": "Filename is empty"}), 400
    if not os.path.isdir(folder):
        return jsonify({"ok": False, "error": "Folder does not exist"}), 400
    full_path = os.path.join(folder, filename)
    service.start_recording(full_path, fmt)
    return jsonify({"ok": True, "path": full_path})


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    service.stop_recording()
    s = service.snapshot()
    return jsonify({"ok": True, "segments_saved": s["segments_saved"], "path": s["save_path"]})


@app.route("/api/params", methods=["POST"])
def api_params():
    body = request.get_json(force=True, silent=True) or {}
    # normalize a friendly axis choice (e.g. "XYZ") into the encoding
    if body.get("axis_select") in AXIS_ENCODINGS:
        body["axis_select"] = AXIS_ENCODINGS[body["axis_select"]]
    for key in ("sampling_frequency", "adc_num_samples", "sleep_dur"):
        if key in body:
            try:
                body[key] = int(body[key])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "%s must be a number" % key}), 400
    if "com_port" in body:
        body["com_port"] = (body["com_port"] or "").strip() or None
    service.apply_params(body)
    config.update({k: v for k, v in body.items()
                   if k in ("sampling_frequency", "adc_num_samples", "axis_select", "sleep_dur", "com_port")})
    save_config(config)
    return jsonify({"ok": True, "active_params": service.snapshot()["active_params"]})


def _open_browser():
    try:
        webbrowser.open("http://127.0.0.1:8000")
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="run with synthetic data, no hardware")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    DEMO_MODE = args.demo
    if not args.no_browser:
        threading.Timer(1.0, _open_browser).start()

    banner = "DEMO (synthetic data)" if DEMO_MODE else "HARDWARE"
    print("=" * 54)
    print(" Voyager3 dashboard  ·  mode: %s" % banner)
    print(" Open http://127.0.0.1:%d" % args.port)
    print("=" * 54)
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
