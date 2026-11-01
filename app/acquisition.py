"""
Voyager3 acquisition service.

Wraps the (Python-3-ported) SmartMeshSDK to:
  - auto-detect the SmartMesh Manager COM port
  - connect and discover the operational mote(s)
  - push sampling parameters to the mote
  - subscribe to uplink data and reassemble per-axis frames
  - convert raw ADC codes to g-values

It exposes a single AcquisitionService object with a thread-safe status
snapshot that the web dashboard polls. All hardware access lives here so the
dashboard never talks to the SDK directly.

A DEMO mode (no hardware required) generates synthetic X/Y/Z frames so the
dashboard can be exercised end-to-end before the dongle is plugged in.
"""

import os
import sys
import time
import math
import queue
import logging
import threading
import traceback

# Make the bundled SmartMeshSDK importable regardless of where we're launched from
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ----------------------------------------------------------------------------
# Verbose console logging
# ----------------------------------------------------------------------------
# Everything the mote/Manager exchange goes through here at INFO/DEBUG so the
# handshake can be watched live in the console. Set VOYAGER3_RAW_IO=1 to also
# dump the raw HDLC/mux-protocol traffic from the SDK's own ApiConnector logger.
LOG = logging.getLogger("voyager3.acquisition")
if not LOG.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    LOG.addHandler(_h)
    # Quiet by default: only meaningful events (COM port, mote found, ready,
    # recording, errors). Set VOYAGER3_VERBOSE=1 for the full per-packet stream.
    _verbose = os.environ.get("VOYAGER3_VERBOSE")
    LOG.setLevel(logging.DEBUG if _verbose else logging.INFO)
    LOG.propagate = False

    if os.environ.get("VOYAGER3_RAW_IO"):
        _api_log = logging.getLogger("ApiConnector")
        _api_log.setLevel(logging.DEBUG)
        _api_log.addHandler(_h)


def _hex(byte_list):
    return " ".join("%02X" % b for b in byte_list)

# ----------------------------------------------------------------------------
# Conversion constants (from Voyager3_app.py)
# ----------------------------------------------------------------------------
VCC          = 3.3
V_PERG_REF   = 0.04
VREF         = 1.8
ADC_RES      = 16
ADC_OFFSET   = 2 ** (ADC_RES - 1)              # 32768
V_PER_LSB    = VCC / 2 ** ADC_RES
V_PERG_VCC   = V_PERG_REF * VCC / VREF
ADC_GAIN     = V_PER_LSB / V_PERG_VCC
F_ADC        = 6.5e6

ADC_PARAM_LEN     = 2        # header words per axis frame (firmware uses 2; see GUI Mote.__init__)
FLASH_PAGE_SAMPLES = 1024    # <= this -> FFT included; above -> raw only

UDP_PORT_NUMBER = 61630      # mote listening port used by dn_sendData (confirm vs firmware)

RMS_TREND_LEN = 20           # rolling window length for the RMS trend plot

# Axis-select ASCII encodings (SmartMesh_RF_cog.h)
AXIS_ENCODINGS = {
    "XYZ": "111", "XY": "110", "XZ": "101", "X": "100",
    "YZ": "11",   "Y": "10",   "Z": "1",
}


def raw_code_to_g(code, axis_offset=0):
    return ADC_GAIN * (code - ADC_OFFSET - axis_offset)


def compute_fft_magnitude(g_vals):
    """Real FFT magnitude (in g) of a time-domain segment. Returns a list of
    N/2+1 bins. Frequency of bin k = k * fs / N (frontend builds the axis)."""
    n = len(g_vals)
    if n < 2:
        return []
    try:
        import numpy as np
        w = np.asarray(g_vals, dtype=float)
        w = w - w.mean()                      # drop DC so the 0-bin doesn't dominate
        mag = np.abs(np.fft.rfft(w)) * (2.0 / n)
        return [round(float(v), 6) for v in mag]
    except Exception:
        # pure-python fallback (slower) — DFT magnitude
        import math as _m
        mean = sum(g_vals) / n
        w = [v - mean for v in g_vals]
        half = n // 2
        out = []
        for k in range(half + 1):
            re = im = 0.0
            for t in range(n):
                ang = -2.0 * _m.pi * k * t / n
                re += w[t] * _m.cos(ang)
                im += w[t] * _m.sin(ang)
            out.append(round((_m.hypot(re, im)) * (2.0 / n), 6))
        return out


def _pad8(value):
    s = str(value)
    return s + ("x" * (8 - len(s)))


def build_sampling_message(sampling_frequency, axis_sel, adc_num_samples,
                           sleep_dur, alarm="0xxxxxxx"):
    """Assemble the 56-byte downlink sampling-parameter packet (cmd 22)."""
    cmd_descriptor = "22xxxxxx"
    final_stage    = "0xxxxxxx"
    msg = (_pad8(sampling_frequency) + alarm + _pad8(axis_sel) +
           _pad8(adc_num_samples) + _pad8(sleep_dur) + cmd_descriptor + final_stage)
    assert len(msg) == 56, "sampling message must be 56 bytes, got %d" % len(msg)
    return msg


class AcquisitionService:
    """Owns the connection to the Manager and the current status snapshot."""

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None

        # --- status snapshot (everything the dashboard shows) ---
        self.status = {
            "mode":           "idle",       # idle | connecting | live | demo | error
            "manager":        "disconnected",
            "com_port":       None,
            "mote_mac":       None,
            "mote_state":     None,         # operational / not found
            "data_flowing":   False,
            "last_packet_ts": None,
            "packets_total":  0,
            "frames_total":   0,
            "last_axis":      None,
            "rms":            {"x": None, "y": None, "z": None},   # per-axis RMS (g)
            "rms_hist":       {"x": [], "y": [], "z": []},          # rolling RMS history
            "wave":           {"x": [], "y": [], "z": []},          # latest time-domain (g)
            "fft":            {"x": [], "y": [], "z": []},          # latest FFT magnitude (g)
            "offset_active":  False,        # True once a stationary baseline is captured
            "active_params":  {
                "sampling_frequency": config.get("sampling_frequency"),
                "adc_num_samples":    config.get("adc_num_samples"),
                "axis_select":        config.get("axis_select"),
                "sleep_dur":          config.get("sleep_dur"),
            },
            "last_error":     None,
            "waveform":       [],           # small rolling buffer for the live strip
            "recording":      False,
            "predicting":     False,
            "prediction":     None,          # {label, confidence, latency_ms, probs, healthy}
            "predict_error":  None,
            "segments_saved": 0,
            "seg_axes":       {"x": False, "y": False, "z": False},  # received in pending segment
            "save_path":      None,         # full path of the file being written
            "save_error":     None,
        }

        self._connector = None
        self._subscriber = None
        self._motes = []
        # per-mac reassembly state
        self._rx = {}
        # per-axis ADC-code offsets (subtracted before g-conversion) and a
        # pending-capture flag set by the "Remove ADC offset" button
        self._offsets = {"x": 0.0, "y": 0.0, "z": 0.0}
        self._offset_capture = {"x": False, "y": False, "z": False}
        self._last_frame_ts = 0.0

        # --- recording state ---
        self._recording = False
        self._rec_path = None
        self._rec_fmt = "csv"
        self._seg = {"x": None, "y": None, "z": None}   # buffered g-values per axis
        self._seg_base_ts = None
        self._rec_expected = ["x", "y", "z"]            # axes we wait for per segment
        self._xlsx_rows = []                            # accumulator for xlsx format

        # --- prediction state ---
        self._predicting = False
        self._pred_thread = None
        self._predictor = None

    # ------------------------------------------------------------------ public
    def snapshot(self):
        with self._lock:
            # shallow copy is enough for the dashboard's JSON serialization
            s = dict(self.status)
            s["rms"] = dict(self.status["rms"])
            s["rms_hist"] = {k: list(v) for k, v in self.status["rms_hist"].items()}
            s["wave"] = {k: list(v) for k, v in self.status["wave"].items()}
            s["fft"] = {k: list(v) for k, v in self.status["fft"].items()}
            s["seg_axes"] = dict(self.status["seg_axes"])
            s["prediction"] = dict(self.status["prediction"]) if self.status["prediction"] else None
            s["active_params"] = dict(self.status["active_params"])
            s["waveform"] = list(self.status["waveform"])
            return s

    def start(self, demo=False):
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        target = self._run_demo if demo else self._run_hardware
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        self._predicting = False
        try:
            if self._subscriber:
                self._subscriber.stop()
        except Exception:
            pass
        try:
            if self._connector:
                self._connector.disconnect()
        except Exception:
            pass
        with self._lock:
            self.status["mode"] = "idle"
            self.status["manager"] = "disconnected"
            self.status["data_flowing"] = False

    def apply_params(self, params):
        """Update config + push new sampling parameters to the mote(s)."""
        with self._lock:
            for k in ("sampling_frequency", "adc_num_samples", "axis_select", "sleep_dur"):
                if k in params:
                    self.config[k] = params[k]
                    self.status["active_params"][k] = params[k]
            if "com_port" in params:
                self.config["com_port"] = params["com_port"]
                self.status["com_port"] = params["com_port"] or "auto (next connect)"
        if self.status["mode"] == "live" and self._connector and self._motes:
            self._send_params_to_all()

    def request_offset_removal(self):
        """Capture the mote's stationary baseline on the next frame of each axis,
        so a stationary sensor reads ~0 g (pure-vibration view)."""
        with self._lock:
            self._offset_capture = {"x": True, "y": True, "z": True}
            self.status["offset_active"] = True

    # ------------------------------------------------------------- prediction
    def start_prediction(self):
        if self._predicting:
            return
        self._predicting = True
        with self._lock:
            self.status["predicting"] = True
            self.status["predict_error"] = None
        self._pred_thread = threading.Thread(target=self._prediction_loop, daemon=True)
        self._pred_thread.start()

    def stop_prediction(self):
        self._predicting = False
        with self._lock:
            self.status["predicting"] = False

    def _prediction_loop(self):
        # Demo mode: fabricate predictions so the UI can be exercised without a model.
        if self.status.get("mode") == "demo":
            import random
            demo_classes = ["Normal", "Faulty"]
            while self._predicting and not self._stop.is_set():
                lbl = random.choices(demo_classes, weights=[5, 1])[0]
                conf = random.uniform(0.82, 0.99)
                probs = {c: round(random.uniform(0, 0.2), 4) for c in demo_classes}
                probs[lbl] = round(conf, 4)
                with self._lock:
                    self.status["prediction"] = {
                        "label": lbl, "confidence": conf,
                        "latency_ms": round(random.uniform(3, 12), 2),
                        "probs": probs, "healthy": lbl == "Normal",
                    }
                time.sleep(0.8)
            return

        # load the predictor once (see app/predictor.py to plug in your own model)
        if self._predictor is None:
            try:
                from app.predictor import Predictor
                self._predictor = Predictor(self.config)
            except Exception as e:
                self._predictor = None
                with self._lock:
                    self.status["predict_error"] = "Could not import model: %s" % e
                    self.status["predicting"] = False
                self._predicting = False
                return

        p = self._predictor
        if not p.ok:
            with self._lock:
                self.status["predict_error"] = "Model load failed: %s" % p.error
                self.status["predicting"] = False
            self._predicting = False
            LOG.error("Prediction disabled: %s", p.error)
            return

        LOG.info("Prediction started (classes=%s)", p.classes)
        while self._predicting and not self._stop.is_set():
            with self._lock:
                wave = {k: list(v) for k, v in self.status["wave"].items()}
            if wave["x"] and wave["y"] and wave["z"]:
                try:
                    t0 = time.time()
                    res = p.predict(wave)
                    res["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
                    res["healthy"] = (res["label"] == p.healthy_label)
                    with self._lock:
                        self.status["prediction"] = res
                except Exception as e:
                    with self._lock:
                        self.status["predict_error"] = "Predict failed: %s" % e
                    LOG.error("Predict failed: %s", e)
            time.sleep(0.5)
        LOG.info("Prediction stopped")

    # ------------------------------------------------------------- recording
    @staticmethod
    def _expected_axes_from_select(axis_select):
        """axis_select like '111','100','10' -> list of enabled axes in x,y,z order."""
        bits = str(axis_select).zfill(3)   # '1' -> '001' (Z only), '11' -> '011' (YZ)
        return [ax for bit, ax in zip(bits, "xyz") if bit == "1"] or ["x", "y", "z"]

    def start_recording(self, full_path, fmt):
        with self._lock:
            self._rec_path = full_path
            self._rec_fmt = (fmt or "csv").lower()
            self._recording = True
            self._seg = {"x": None, "y": None, "z": None}
            self._seg_base_ts = None
            self._xlsx_rows = []
            self._rec_expected = self._expected_axes_from_select(self.config.get("axis_select", "111"))
            self.status["recording"] = True
            self.status["segments_saved"] = 0
            self.status["seg_axes"] = {"x": False, "y": False, "z": False}
            self.status["save_path"] = full_path
            self.status["save_error"] = None
        # write CSV/TXT header up front
        try:
            if self._rec_fmt in ("csv", "txt"):
                sep = "," if self._rec_fmt == "csv" else "\t"
                with open(full_path, "w", newline="") as f:
                    f.write(sep.join(["sample_index", "timestamp", "x", "y", "z"]) + "\n")
        except Exception as e:
            self._note_save_error("Could not open file: %s" % e)
        LOG.info("Recording started -> %s (fmt=%s, axes=%s)",
                 full_path, self._rec_fmt, self._rec_expected)

    def stop_recording(self):
        with self._lock:
            was = self._recording
            self._recording = False
            self.status["recording"] = False
            rows = list(self._xlsx_rows)
            path = self._rec_path
            fmt = self._rec_fmt
        if was and fmt == "xlsx" and path:
            self._flush_xlsx(path, rows)
        LOG.info("Recording stopped (%d segments saved)", self.status["segments_saved"])

    def _note_save_error(self, msg):
        LOG.error(msg)
        with self._lock:
            self.status["save_error"] = msg

    def _record_axis(self, axis, g_vals, base_ts):
        """Buffer one axis of a segment; when all expected axes are in, write it."""
        if axis not in self._rec_expected:
            return
        self._seg[axis] = g_vals
        if self._seg_base_ts is None:
            self._seg_base_ts = base_ts
        with self._lock:
            self.status["seg_axes"][axis] = True

        if all(self._seg[a] is not None for a in self._rec_expected):
            self._write_segment()
            self._seg = {"x": None, "y": None, "z": None}
            self._seg_base_ts = None
            with self._lock:
                self.status["segments_saved"] += 1
                self.status["seg_axes"] = {"x": False, "y": False, "z": False}

    def _write_segment(self):
        fs = float(self.config.get("sampling_frequency") or 1) or 1.0
        base = self._seg_base_ts or time.time()
        # rows aligned by sample index; axes not enabled are written blank
        length = max((len(self._seg[a]) for a in self._rec_expected if self._seg[a]), default=0)
        rows = []
        for i in range(length):
            ts = base + i / fs
            x = self._seg["x"][i] if self._seg["x"] and i < len(self._seg["x"]) else ""
            y = self._seg["y"][i] if self._seg["y"] and i < len(self._seg["y"]) else ""
            z = self._seg["z"][i] if self._seg["z"] and i < len(self._seg["z"]) else ""
            rows.append([i, round(ts, 6),
                         round(x, 6) if x != "" else "",
                         round(y, 6) if y != "" else "",
                         round(z, 6) if z != "" else ""])
        try:
            if self._rec_fmt in ("csv", "txt"):
                sep = "," if self._rec_fmt == "csv" else "\t"
                with open(self._rec_path, "a", newline="") as f:
                    for r in rows:
                        f.write(sep.join(str(v) for v in r) + "\n")
            elif self._rec_fmt == "xlsx":
                self._xlsx_rows.extend(rows)
                self._flush_xlsx(self._rec_path, self._xlsx_rows)
        except Exception as e:
            self._note_save_error("Write failed: %s" % e)

    def _flush_xlsx(self, path, rows):
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["sample_index", "timestamp", "x", "y", "z"])
            for r in rows:
                ws.append(r)
            wb.save(path)
        except Exception as e:
            self._note_save_error("XLSX save failed (is openpyxl installed?): %s" % e)


    # ------------------------------------------------------------- status helr
    def _set(self, **kw):
        with self._lock:
            self.status.update(kw)

    def _note_error(self, msg):
        LOG.error(msg)
        with self._lock:
            self.status["last_error"] = msg
            self.status["mode"] = "error"

    def _push_rms(self, axis, rms_value):
        with self._lock:
            self.status["rms"][axis] = round(rms_value, 6)
            self.status["last_axis"] = axis
            self.status["last_packet_ts"] = time.time()
            self.status["data_flowing"] = True

    # ------------------------------------------------------------- hardware run
    def _run_hardware(self):
        try:
            from SmartMeshSDK.IpMgrConnectorSerial import IpMgrConnectorSerial
            from SmartMeshSDK.IpMgrConnectorMux import IpMgrSubscribe
        except Exception as e:
            self._note_error("Could not import SmartMeshSDK: %s" % e)
            return

        self._set(mode="connecting", manager="connecting")

        # 1) find COM port
        port = self.config.get("com_port") or self._autodetect_port()
        if not port:
            self._note_error("No COM ports found. Is the Manager plugged in?")
            return
        LOG.info("Using COM port: %s", port)
        self._set(com_port=port)

        # 2) connect
        try:
            self._connector = IpMgrConnectorSerial.IpMgrConnectorSerial()
            self._connector.connect({"port": port})
        except Exception as e:
            self._note_error("Connection to Manager failed on %s: %s" % (port, e))
            return
        LOG.info("Connected to Manager on %s", port)
        self._set(manager="connected")

        # 3) discover motes
        LOG.info("Discovering motes (dn_getMoteConfig walk)...")
        try:
            self._motes = self._discover_motes()
        except Exception as e:
            self._note_error("Mote discovery failed: %s" % e)
            return

        if not self._motes:
            LOG.warning("No operational motes found (state==4). See per-mote "
                        "states logged above -- if your mote shows a different "
                        "state it is joined but not yet operational.")
            self._set(mote_state="not found")
        else:
            LOG.info("Operational motes: %s", [_pretty_mac(m) for m in self._motes])
            self._set(mote_mac=_pretty_mac(self._motes[0]), mote_state="operational")
            for mac in self._motes:
                self._rx[tuple(mac)] = {"buf": None, "cnt": 0,
                                         "mote_port": None, "host_port": None}

        # 4) subscribe to data
        try:
            self._subscriber = IpMgrSubscribe.IpMgrSubscribe(self._connector)
            self._subscriber.start()
            self._subscriber.subscribe(
                notifTypes=[IpMgrSubscribe.IpMgrSubscribe.NOTIFDATA],
                fun=self._on_notifdata,
                isRlbl=False,
            )
        except Exception as e:
            self._note_error("Subscribe failed: %s" % e)
            return
        LOG.info("Subscribed to NOTIFDATA notifications; UDP_PORT_NUMBER=%d "
                  "(used as both srcPort and dstPort on dn_sendData)", UDP_PORT_NUMBER)

        # 5) go live and WAIT. We must not send anything yet: the mote drives the
        #    handshake by advertising 0xAA BB CC DD when it is ready. We reply with
        #    params at that point (see _handle_bytes), which is what starts sampling.
        self._set(mode="live")
        LOG.info("Live. Waiting for mote ready-advert (AA BB CC DD)...")

        # keep alive; watch for data-flow stalls
        was_flowing = False
        while not self._stop.is_set():
            time.sleep(1.0)
            with self._lock:
                lp = self.status["last_packet_ts"]
                if lp and (time.time() - lp) > 5:
                    self.status["data_flowing"] = False
                is_flowing = self.status["data_flowing"]
            if was_flowing and not is_flowing:
                LOG.warning("Data flow stalled: no packet received in the last 5s.")
            was_flowing = is_flowing

    def _autodetect_port(self):
        """Pick the Manager's API port: the highest-numbered of its FTDI (VID
        0x0403) ports. Falls back to the highest COM if no FTDI port is found
        -- Bluetooth serial links must not win just by having a higher number."""
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            ftdi = [p for p in ports if p.vid == 0x0403]
            if ftdi:
                ports = ftdi
            if not ports:
                return None

            def port_num(p):
                digits = "".join(ch for ch in p.device if ch.isdigit())
                return int(digits) if digits else -1

            ports.sort(key=port_num)
            return ports[-1].device
        except Exception as e:
            self._note_error("COM auto-detect error: %s" % e)
            return None

    def _discover_motes(self):
        macs = []
        current = (0, 0, 0, 0, 0, 0, 0, 0)
        from SmartMeshSDK.ApiException import APIError
        while True:
            try:
                res = self._connector.dn_getMoteConfig(current, True)
            except APIError as e:
                LOG.debug("dn_getMoteConfig walk ended: %s", e)
                break
            except Exception as e:
                LOG.debug("dn_getMoteConfig walk ended (error): %s", e)
                break
            LOG.info("Mote %s: isAP=%s state=%s", _pretty_mac(res.macAddress),
                      res.isAP, res.state)
            if (not res.isAP) and (res.state in [4]):
                macs.append(res.macAddress)
            current = res.macAddress
        return macs

    def _send_params_to_all(self, mac=None):
        """Send the cmd-22 sampling-parameter packet. This is the ONLY thing the
        PC ever sends to the mote: it both starts sampling (in response to the
        mote's ready advert) and, re-sent at each frame start, keeps it going."""
        msg = build_sampling_message(
            self.config["sampling_frequency"],
            self.config["axis_select"],
            self.config["adc_num_samples"],
            self.config["sleep_dur"],
        )
        data = [ord(c) for c in msg]
        targets = [mac] if mac is not None else list(self._motes)
        LOG.debug("Sending cmd-22 params to %s: msg=%r (%d bytes) hex=%s",
                  [_pretty_mac(m) for m in targets], msg, len(data), _hex(data))
        for m in targets:
            # Prefer the port pair learned from this mote's own traffic (its
            # srcPort becomes our dstPort, its dstPort becomes our srcPort) --
            # UDP_PORT_NUMBER is only a fallback for a mote we haven't heard
            # from yet, and on observed firmware does NOT match the real ports.
            st = self._rx.get(tuple(m))
            mote_port = st.get("mote_port") if st else None
            host_port = st.get("host_port") if st else None
            src_port = host_port if host_port is not None else UDP_PORT_NUMBER
            dst_port = mote_port if mote_port is not None else UDP_PORT_NUMBER
            try:
                res = self._connector.dn_sendData(
                    macAddress=m, priority=0,
                    srcPort=src_port, dstPort=dst_port,
                    options=0, data=data,
                )
                LOG.debug("dn_sendData -> %s (srcPort=%d dstPort=%d): RC=%s callbackId=%s",
                          _pretty_mac(m), src_port, dst_port, res.RC, res.callbackId)
            except Exception as e:
                LOG.error("Send params to %s (srcPort=%d dstPort=%d) failed: %s",
                          _pretty_mac(m), src_port, dst_port, e)
                self._note_error("Send params failed: %s" % e)

    def _on_notifdata(self, notifName, notifParams):
        try:
            mac = tuple(notifParams.macAddress)
            data = list(notifParams.data)
            LOG.debug("RX from %s srcPort=%d dstPort=%d len=%d hex=%s",
                      _pretty_mac(mac), notifParams.srcPort, notifParams.dstPort,
                      len(data), _hex(data))
            with self._lock:
                self.status["packets_total"] += 1
            if mac not in self._rx:
                # A mote that joined after discovery: adopt it rather than
                # silently dropping its traffic until the next reconnect.
                LOG.info("Adopting late-joining mote %s", _pretty_mac(mac))
                self._motes.append(mac)
                self._rx[mac] = {"buf": None, "cnt": 0,
                                 "mote_port": None, "host_port": None}
                self._set(mote_mac=_pretty_mac(mac), mote_state="operational")
            # The mote's own UDP port pair is only knowable from its traffic (it
            # doesn't match the UDP_PORT_NUMBER constant on this firmware build) --
            # learn it here so _send_params_to_all can reply on the right ports.
            st = self._rx[mac]
            if st.get("mote_port") != notifParams.srcPort or st.get("host_port") != notifParams.dstPort:
                LOG.info("Learned port pair for %s: mote_port=%d host_port=%d",
                          _pretty_mac(mac), notifParams.srcPort, notifParams.dstPort)
            st["mote_port"] = notifParams.srcPort
            st["host_port"] = notifParams.dstPort
            try:
                self._last_frame_ts = notifParams.utcSecs + 1e-6 * notifParams.utcUsecs
            except Exception:
                self._last_frame_ts = time.time()
            self._handle_bytes(mac, data)
        except Exception:
            LOG.error("Exception in _on_notifdata:\n%s", traceback.format_exc())
            self._note_error(traceback.format_exc().splitlines()[-1])

    def _handle_bytes(self, mac, byte_list):
        # Per-axis frame length = ADC_PARAM_LEN + N raw + N/2 FFT  (= 3N/2 + 2).
        # For N > 1024 the mote streams raw only (no FFT region).
        n = self.config["adc_num_samples"]
        if n <= FLASH_PAGE_SAMPLES:
            expected = ADC_PARAM_LEN + n + n // 2
        else:
            expected = ADC_PARAM_LEN + n
        st = self._rx[mac]
        if st["buf"] is None or len(st["buf"]) != expected:
            st["buf"] = [0] * expected
            st["cnt"] = 0

        # --- mote ready advert: 0xAA 0xBB 0xCC 0xDD ---
        # The mote drives the handshake. When it advertises ready we reply with
        # the params packet, which starts sampling.
        if byte_list[:4] == [170, 187, 204, 221]:
            LOG.info("READY ADVERT (AA BB CC DD) from %s -> sending params to start sampling",
                      _pretty_mac(mac))
            self._send_params_to_all(mac)
            return

        buf = st["buf"]
        data_len = len(byte_list) // 2
        msb_set = len(byte_list) > 1 and byte_list[1] == 255   # alignment bit

        # resync: got more than a frame's worth -> drop and restart
        if st["cnt"] + data_len > len(buf):
            LOG.warning("Resync (overflow) from %s: cnt=%d + data_len=%d > expected=%d -> restart",
                        _pretty_mac(mac), st["cnt"], data_len, len(buf))
            st["cnt"] = 0
        # resync: alignment bit arrived mid-frame -> restart
        if st["cnt"] > 0 and msb_set:
            LOG.warning("Resync (mid-frame align bit) from %s at cnt=%d -> restart",
                        _pretty_mac(mac), st["cnt"])
            st["cnt"] = 0
        # not yet aligned to a frame start -> wait
        if st["cnt"] == 0 and not msb_set:
            LOG.debug("Not frame-aligned yet from %s (no MSB align bit), dropping %d bytes",
                      _pretty_mac(mac), len(byte_list))
            return

        # START OF FRAME: re-send params. This is the keep-alive handshake --
        # if it stops, the firmware stops sampling.
        if msb_set and st["cnt"] == 0:
            LOG.debug("Start of frame from %s -> re-sending params (keep-alive)",
                      _pretty_mac(mac))
            self._send_params_to_all(mac)

        # assemble 16-bit words (little-endian: low byte first)
        for i in range(0, len(byte_list) - 1, 2):
            if st["cnt"] >= len(buf):
                break
            buf[st["cnt"]] = (byte_list[i + 1] << 8) | byte_list[i]
            st["cnt"] += 1

        LOG.debug("Frame assembly for %s: %d/%d words", _pretty_mac(mac), st["cnt"], len(buf))

        if st["cnt"] >= len(buf):
            frame = buf
            st["buf"] = None
            st["cnt"] = 0
            LOG.debug("Frame complete from %s (%d words) -> consuming", _pretty_mac(mac), len(frame))
            self._consume_frame(frame)

    def _consume_frame(self, frame):
        header = frame[0]
        n = self.config["adc_num_samples"]
        raw = frame[ADC_PARAM_LEN:n]          # matches GUI: data[2:N]
        axis_bits = int(header) & 0x00F0
        axis = {144: "x", 160: "y", 192: "z"}.get(axis_bits)
        with self._lock:
            self.status["frames_total"] += 1
        if not (axis and raw):
            LOG.warning("Frame header=0x%02X did not decode to a known axis "
                        "(axis_bits=0x%02X) or had no samples", header, axis_bits)
            return

        # If the user asked to remove offset, capture this axis's stationary
        # baseline (mean code minus mid-scale) so g reads ~0 when stationary.
        if self._offset_capture.get(axis):
            mean_code = sum(raw) / len(raw)
            self._offsets[axis] = mean_code - ADC_OFFSET
            self._offset_capture[axis] = False
            LOG.info("Captured %s-axis offset = %.1f codes", axis, self._offsets[axis])

        off = self._offsets[axis]
        g_vals = [raw_code_to_g(c, off) for c in raw]
        # RMS = sqrt(mean(g^2)) over the frame -> overall vibration level (g)
        rms = math.sqrt(sum(v * v for v in g_vals) / len(g_vals))
        fft_mag = compute_fft_magnitude(g_vals)
        LOG.debug("Frame decoded: axis=%s header=0x%02X samples=%d rms=%.6f g",
                 axis, header, len(raw), rms)
        with self._lock:
            self.status["wave"][axis] = [round(v, 6) for v in g_vals]
            self.status["fft"][axis] = fft_mag
            h = self.status["rms_hist"][axis]
            h.append(round(rms, 6))
            if len(h) > RMS_TREND_LEN:
                del h[:len(h) - RMS_TREND_LEN]
        self._push_rms(axis, rms)
        if self._recording:
            self._record_axis(axis, g_vals, self._last_frame_ts)

    # ----------------------------------------------------------------- demo run
    def _run_demo(self):
        """Synthetic data so the dashboard can be seen working without hardware."""
        self._set(mode="connecting", manager="connecting", com_port="DEMO")
        time.sleep(0.6)
        self._set(manager="connected", mote_mac="00-17-0D-00-00-DE-M0", mote_state="operational")
        time.sleep(0.4)
        self._set(mode="demo")
        axes = ["x", "y", "z"]
        t = 0.0
        ai = 0
        while not self._stop.is_set():
            axis = axes[ai % 3]
            ai += 1
            base = {"x": 0.02, "y": 0.015, "z": 0.05}[axis]
            # synthetic 512-pt segment: a couple of tones + noise
            import random
            f1 = {"x": 30, "y": 55, "z": 18}[axis]
            seg = [base * math.sin(2*math.pi*f1*(k/512.0))
                   + 0.3*base * math.sin(2*math.pi*120*(k/512.0))
                   + 0.15*base*random.uniform(-1, 1) for k in range(512)]
            rms = math.sqrt(sum(v*v for v in seg)/len(seg))
            fft_mag = compute_fft_magnitude(seg)
            with self._lock:
                self.status["wave"][axis] = [round(v, 6) for v in seg]
                self.status["fft"][axis] = fft_mag
                h = self.status["rms_hist"][axis]
                h.append(round(rms, 6))
                if len(h) > RMS_TREND_LEN:
                    del h[:len(h) - RMS_TREND_LEN]
            self._push_rms(axis, rms)
            if self._recording:
                self._record_axis(axis, seg[:64], time.time())
            with self._lock:
                self.status["packets_total"] += 1
                self.status["frames_total"] += 1
            t += 0.12
            time.sleep(0.5)


def _pretty_mac(mac):
    return "-".join("%02X" % b for b in mac)
