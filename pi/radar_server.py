#!/usr/bin/env python3
"""Reads an LD2410C over UART and serves a live radar web page.

    python3 radar_server.py                      # /dev/serial0 @ 256000, http://<pi>:8080
    python3 radar_server.py --serial /dev/ttyUSB0
    python3 radar_server.py --demo               # simulated person, no sensor needed

Only needs the standard library plus pyserial (apt: python3-serial).
"""
import argparse
import json
import logging
import math
import queue
import random
import statistics
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ld2410 as L

WEB_DIR = Path(__file__).resolve().with_name("web")
log = logging.getLogger("radar")


class PersonTrack:
    """One smooth person track (distance + velocity) fused from everything the sensor reports.

    Why: walking away, the echo weakens fast, so the sensor's own moving target drops out past ~2 m and
    it freezes its stationary distance for seconds at a time. Each frame is matched, in order, to:
      1. the sensor's moving target, when active
      2. a peak in per-gate moving energy clearly above that gate's own noise (gate g is centred on
         g * gate_m), near where the track already is - this follows you after the sensor gives up
      3. the sensor's stationary distance while it is still changing (fresh)
      4. a frozen stationary distance, only as a slow pull once nothing better has arrived for a while
    An alpha-beta filter keeps position and velocity; between measurements it coasts on a decaying
    velocity, never more than one gate from where it was last measured.
    """

    def __init__(self):
        self.pos = self.vel = None
        self.anchor = None
        self.t = self.last_motion = self.last_meas = self.last_present = 0.0
        self.mu = self.sig = None
        self.sta_last, self.sta_changed = None, 0.0
        self.sta_out_since = self.sta_out_val = None
        self.source = None

    def _gate_peak(self, energies, gate_m, near):
        n = len(energies)
        if self.mu is None or len(self.mu) != n:
            self.mu, self.sig = [float(e) for e in energies], [6.0] * n
        excess = []
        for g, e in enumerate(energies):
            m, s = self.mu[g], self.sig[g]
            thr = m + max(10.0, 3.0 * s)
            if e < thr:  # learn each gate's noise only from frames where it isn't detecting anything
                self.mu[g] = m + 0.02 * (e - m)
                self.sig[g] = s + 0.02 * (abs(e - m) - s)
            excess.append(e - thr if e >= thr else 0.0)
        # gates 0-1 are noisy, and close-range motion is strong enough for the sensor's own moving target
        cands = [g for g in range(2, n) if excess[g] > 0 and (near is None or abs(g * gate_m - near) <= 1.5 * gate_m)]
        if not cands:
            return None
        g = max(cands, key=excess.__getitem__)
        lo, hi = max(0, g - 1), min(n - 1, g + 1)
        w = [max(0.0, energies[i] - self.mu[i]) for i in range(lo, hi + 1)]
        return sum((lo + i) * wi for i, wi in enumerate(w)) / sum(w) * gate_m

    def update(self, frame, cfg, t=None):
        t = time.time() if t is None else t
        gate_m = cfg.get("gate_m", L.GATE_METERS)
        span = ((cfg.get("max_gate") or 8) + 0.5) * gate_m
        dt = min(0.5, max(0.02, t - self.t)) if self.t else 0.1
        self.t = t

        if self.pos is not None:  # predict, but coast at most one gate away from the last measurement
            self.pos += self.vel * dt
            self.vel *= math.exp(-dt / 0.6)
            self.pos = max(self.anchor - gate_m, min(self.anchor + gate_m, self.pos))

        mov, sta = frame["moving"], frame["stationary"]
        zs = sta["distance_cm"] / 100 if sta["active"] else None
        if zs is not None and zs != self.sta_last:
            self.sta_last, self.sta_changed = zs, t
        fresh = zs is not None and t - self.sta_changed < 1.5
        # the sensor sometimes blips a far-off stationary reading (often 0.30 m); trust one only if it persists
        if fresh and self.pos is not None and abs(zs - self.pos) > 1.5 * gate_m:
            if self.sta_out_since is None or abs(zs - self.sta_out_val) > 0.3:
                self.sta_out_since, self.sta_out_val = t, zs
            fresh = t - self.sta_out_since >= 0.7
        else:
            self.sta_out_since = None
        if frame["state"]:
            self.last_present = t
        near = self.pos if self.pos is not None and t - self.last_meas < 3 else None
        peak = self._gate_peak(frame["gates_moving"], gate_m, near) if frame.get("gates_moving") else None
        if near is None and not frame["state"]:
            peak = None  # gate peaks only continue a track the sensor started; they never invent a person

        z = kind = None
        if mov["active"]:
            z, kind, a, b = mov["distance_cm"] / 100, "moving", 0.5, 0.08
        elif peak is not None:
            z, kind, a, b = peak, "gates", 0.35, 0.05
        elif fresh:
            z, kind, a, b = zs, "stationary", 0.35, 0.0
        elif zs is not None and t - self.last_meas > 2.5:
            z, kind, a, b = zs, "stale", 0.08, 0.0

        if z is not None:
            if self.pos is None:
                self.pos, self.vel = z, 0.0
            else:
                r = z - self.pos
                self.pos += a * r
                self.vel = max(-1.5, min(1.5, self.vel + b * r / dt))
                if b == 0:
                    self.vel *= 0.7
            if kind != "stale":
                self.last_meas = t
                self.anchor = self.pos
            else:
                self.anchor = self.pos if self.anchor is None else self.anchor + 0.08 * (self.pos - self.anchor)
            if kind in ("moving", "gates"):
                self.last_motion = t
            self.source = kind
        elif self.pos is not None:
            self.source = "coast"

        present = frame["state"] != 0 or (t - self.last_motion < 1.0 and t - self.last_present < 3)
        if not present:
            self.pos = self.vel = self.anchor = None
            return None
        if self.pos is None:
            return None
        self.pos = max(0.0, min(span, self.pos))
        return {"distance_m": round(self.pos, 2), "velocity": round(self.vel, 2), "source": self.source,
                "confident": self.source in ("moving", "gates", "stationary")}


class Tracker:
    """Per-frame estimate: the fused person track, plus lightly smoothed raw moving / stationary targets.

    A raw target is "confident" only if its energy clears the trigger threshold of the gate it is in;
    otherwise it is usually a stale or ghost reading and the UI draws it faintly.
    """

    def __init__(self):
        self.hist = {k: deque(maxlen=5) for k in ("moving", "stationary")}
        self.ema = {"moving": None, "stationary": None}
        self.last_seen = {"moving": 0.0, "stationary": 0.0}
        self.person = PersonTrack()

    def update(self, frame, cfg, now=None):
        now = time.time() if now is None else now
        gate_m = cfg.get("gate_m", L.GATE_METERS)
        out = {"person": self.person.update(frame, cfg, now)}
        for kind, th_key in (("moving", "move_thresh"), ("stationary", "static_thresh")):
            t = frame[kind]
            if not t["active"]:
                if now - self.last_seen[kind] > 1.0:
                    self.hist[kind].clear()
                    self.ema[kind] = None
                out[kind] = None
                continue
            self.last_seen[kind] = now
            d = t["distance_cm"] / 100
            self.hist[kind].append(d)
            med = statistics.median(self.hist[kind])
            ema = self.ema[kind]
            ema = med if ema is None else ema + 0.4 * (med - ema)
            self.ema[kind] = ema
            thresholds = cfg.get(th_key) or []
            gate = min(round(d / gate_m), max(len(thresholds) - 1, 0))  # gates are centred on g * gate_m
            th = thresholds[gate] if thresholds else None
            out[kind] = {"distance_m": round(ema, 2), "energy": t["energy"], "gate": gate,
                         "threshold": th, "confident": th is None or t["energy"] >= th}
        return out


class Hub:
    """Latest reading, sensor config and calibration progress; SSE clients wait on the condition."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.frame = None
        self.estimate = None
        self.frame_time = 0.0
        self.status = {"connected": False, "source": "", "error": ""}
        self.config = {}
        self.calib = {"phase": "idle"}
        self.tracker = Tracker()
        self.frames_seen = 0

    def _changed(self):
        self.seq += 1
        self.cond.notify_all()

    def publish(self, frame):
        with self.cond:
            self.frame = frame
            self.estimate = self.tracker.update(frame, self.config)
            self.frame_time = time.time()
            self.frames_seen += 1
            self.status.update(connected=True, error="")
            self._changed()

    def set_status(self, **kw):
        with self.cond:
            self.status.update(kw)
            self._changed()

    def set_config(self, cfg):
        with self.cond:
            self.config.update(cfg)
            self._changed()

    def set_calib(self, **kw):
        with self.cond:
            self.calib = kw
            self._changed()

    def snapshot(self):
        return {"status": dict(self.status), "config": dict(self.config), "calib": dict(self.calib),
                "frame": self.frame, "estimate": self.estimate, "frame_time": self.frame_time,
                "age": round(time.time() - self.frame_time, 2) if self.frame else None}

    def wait(self, seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != seq, timeout)
            return self.seq, self.snapshot()


class Job:
    def __init__(self, op, kw):
        self.op, self.kw = op, kw
        self.done = threading.Event()
        self.result = self.error = None


class SensorBase:
    """Settings changes are queued and run by the sensor thread, which owns the serial port."""

    def __init__(self, hub):
        self.hub = hub
        self.jobs = queue.Queue()

    def do(self, op, timeout=20, **kw):
        job = Job(op, kw)
        self.jobs.put(job)
        if not job.done.wait(timeout):
            raise RuntimeError("sensor did not respond (is it connected?)")
        if job.error:
            raise RuntimeError(job.error)
        return job.result

    def run_pending(self):
        try:
            job = self.jobs.get_nowait()
        except queue.Empty:
            return
        try:
            job.result = getattr(self, "op_" + job.op)(**job.kw)
        except Exception as e:
            log.warning("%s failed: %s", job.op, e)
            job.error = str(e)
        job.done.set()


class SerialSensor(SensorBase):
    def __init__(self, hub, port, baud, engineering):
        super().__init__(hub)
        self.port, self.baud, self.engineering = port, baud, engineering
        self.ser = None
        self.parser = L.Parser()

    def run(self):
        import serial  # imported here so --demo works without pyserial

        src = f"{self.port} @ {self.baud}"
        while True:
            try:
                with serial.Serial(self.port, self.baud, timeout=0.1) as ser:
                    self.ser, self.parser = ser, L.Parser()
                    self.hub.set_status(source=src, error="Waiting for sensor frames...")
                    try:
                        self.setup()
                    except Exception as e:  # data still streams even if config mode fails
                        log.warning("setup: %s", e)
                    last = time.time()
                    while True:
                        self.run_pending()
                        data = ser.read(max(ser.in_waiting, 1))
                        for kind, obj in self.parser.feed(data) if data else ():
                            if kind == "data":
                                self.hub.publish(obj)
                                last = time.time()
                        if time.time() - last > 3:
                            self.hub.set_status(connected=False,
                                                error="No frames from sensor - check TX/RX wiring, power and baud rate")
                            last = time.time()
            except Exception as e:  # missing port, unplugged adapter, permission denied...
                log.warning("serial: %s", e)
                self.hub.set_status(connected=False, source=src, error=str(e))
                time.sleep(2)

    # --- config-mode plumbing ---
    def wait_ack(self, word, timeout):
        end = time.time() + timeout
        while time.time() < end:
            data = self.ser.read(max(self.ser.in_waiting, 1))
            for kind, obj in self.parser.feed(data) if data else ():
                if kind == "ack" and obj["cmd"] == word:
                    return obj
        return None

    def transact(self, cmds, end=True):
        """Run commands inside one config session; returns one ACK (or None) per command."""
        ack = None
        for _ in range(3):  # the first command is sometimes lost while the sensor is streaming
            self.ser.write(L.ENABLE_CONFIG)
            ack = self.wait_ack(0x00FF, 0.5)
            if ack:
                break
        if not ack or not ack["ok"]:
            raise RuntimeError("sensor did not enter config mode")
        acks = []
        try:
            for c in cmds:
                self.ser.write(c)
                acks.append(self.wait_ack(L.command_word(c), 1.0))
        finally:
            if end:
                self.ser.write(L.END_CONFIG)
                self.wait_ack(0x00FE, 0.5)
        return acks

    @staticmethod
    def check(acks, what):
        if not all(a and a["ok"] for a in acks):
            raise RuntimeError(f"sensor rejected {what}")

    def setup(self):
        cmds = [L.READ_FIRMWARE, L.READ_RESOLUTION, L.READ_PARAMS]
        if self.engineering:
            cmds.append(L.ENGINEERING_ON)  # resets on power loss, so send every time
        fw, res, params = self.transact(cmds)[:3]
        cfg = {"firmware": L.parse_firmware(fw["data"]) if fw and fw["ok"] else None,
               "resolution_supported": bool(res and res["ok"]),
               "gate_m": L.parse_resolution(res["data"]) if res and res["ok"] else L.GATE_METERS}
        if params and params["ok"]:
            cfg.update(L.parse_params(params["data"]))
        log.info("sensor config: %s", cfg)
        self.hub.set_config(cfg)

    def reread_params(self, acks):
        p = acks[-1]
        if p and p["ok"]:
            self.hub.set_config(L.parse_params(p["data"]))

    def restart(self):
        self.transact([L.RESTART], end=False)
        time.sleep(2.5)
        self.ser.reset_input_buffer()
        self.parser = L.Parser()
        self.setup()

    # --- operations requested from the web UI ---
    def op_set_sensitivity(self, move, static):
        cmds = [L.set_sensitivity(g, int(m), int(s)) for g, (m, s) in enumerate(zip(move, static))]
        acks = self.transact(cmds + [L.READ_PARAMS])
        self.reread_params(acks)
        # gates 0-1 have no stationary detection and some firmware refuses them; READ_PARAMS shows the truth
        self.check(acks[2:-1], "the new thresholds")

    def op_set_hold(self, seconds):
        cfg = self.hub.config
        acks = self.transact([L.set_max_gates(cfg.get("max_move_gate", 8), cfg.get("max_static_gate", 8),
                                              int(seconds)), L.READ_PARAMS])
        self.check(acks[:1], "the hold time")
        self.reread_params(acks)

    def op_set_resolution(self, fine):
        acks = self.transact([L.set_resolution(fine)])
        self.check(acks, "0.2 m gates (needs firmware 2.44 or newer)" if fine else "0.75 m gates")
        self.restart()

    def op_factory_reset(self):
        self.check(self.transact([L.FACTORY_RESET]), "factory reset")
        self.restart()


class DemoSensor(SensorBase):
    """Fake a person wandering in front of the sensor; honours thresholds, hold time and gate size."""

    def __init__(self, hub):
        super().__init__(hub)
        hub.set_status(source="demo (simulated)")
        hub.set_config({"firmware": "V2.44.demo", "resolution_supported": True, "gate_m": 0.75,
                        "max_gate": 8, "max_move_gate": 8, "max_static_gate": 8, "hold_s": 5,
                        "move_thresh": list(L.DEFAULT_MOVE_THRESH), "static_thresh": list(L.DEFAULT_STATIC_THRESH)})

    def op_set_sensitivity(self, move, static):
        self.hub.set_config({"move_thresh": [int(v) for v in move], "static_thresh": [int(v) for v in static]})

    def op_set_hold(self, seconds):
        self.hub.set_config({"hold_s": int(seconds)})

    def op_set_resolution(self, fine):
        time.sleep(1)
        self.hub.set_config({"gate_m": 0.2 if fine else 0.75})

    def op_factory_reset(self):
        time.sleep(1)
        self.hub.set_config({"gate_m": 0.75, "hold_s": 5, "move_thresh": list(L.DEFAULT_MOVE_THRESH),
                             "static_thresh": list(L.DEFAULT_STATIC_THRESH)})

    def run(self):
        pos, target, pause_until, away_until = 2.0, 4.5, 0.0, 0.0
        last_move = last_static = 0.0
        while True:
            self.run_pending()
            cfg = self.hub.config
            gate_m = cfg["gate_m"]
            span = 9 * gate_m
            now = time.time()
            present = now >= away_until
            moving = False
            if present and now >= pause_until:
                step = 0.07 * (gate_m / 0.75) ** 0.5
                if abs(target - pos) <= step:
                    pos = target
                    target = random.uniform(0.1, 0.92) * span
                    if random.random() < 0.6:
                        pause_until = now + random.uniform(2, 6)
                    elif random.random() < 0.15:
                        away_until = now + random.uniform(4, 8)
                else:
                    pos += step if target > pos else -step
                    moving = True
            pos = min(pos, span * 0.95)

            def gates(peak, floor, near_noise=0):
                centre, width = pos / gate_m, max(0.7, 0.45 / gate_m)
                return [min(100, int(peak * math.exp(-((g + 0.5 - centre) ** 2) / width)
                                     + random.uniform(0, floor) + (near_noise if g == 0 else 0)))
                        for g in range(9)]

            g_mov = gates(random.uniform(60, 100) if present and moving else 0, 12, 14)
            g_sta = gates(random.uniform(35, 70) if present else 0, 10)
            g_sta[0] = g_sta[1] = 0  # the real sensor never reports static energy for gates 0-1
            if any(e > t for e, t in zip(g_mov, cfg["move_thresh"])):
                last_move = now
            if any(e > t for e, t in zip(g_sta[2:], cfg["static_thresh"][2:])):
                last_static = now
            hold = cfg["hold_s"]
            mov_active = now - last_move < min(hold, 1.0)
            sta_active = now - last_static < hold
            state = (1 if mov_active else 0) | (2 if sta_active else 0)
            jitter = lambda: max(0, int(pos * 100 + random.uniform(-0.6, 0.6) * gate_m * 100))
            self.hub.publish({
                "mode": "engineering", "state": state, "state_name": L.STATE_NAMES[state],
                "moving": {"distance_cm": jitter() if mov_active else 0, "energy": max(g_mov), "active": mov_active},
                "stationary": {"distance_cm": jitter() if sta_active else 0, "energy": max(g_sta), "active": sta_active},
                "detect_cm": jitter() if state else 0,
                "gates_moving": g_mov, "gates_static": g_sta,
                "light": random.randint(80, 90), "out": 1 if state else 0,
            })
            time.sleep(0.1)


class ReplaySensor(SensorBase):
    """Plays back a recording (JSON lines of {"t", "frame", "config"}) in real time, looping."""

    def __init__(self, hub, path):
        super().__init__(hub)
        self.rows = [json.loads(line) for line in open(path)]
        hub.set_status(source=f"replay of {Path(path).name}")
        hub.set_config(self.rows[0].get("config") or {})

    def _read_only(self, **_):
        raise RuntimeError("settings can't be changed while replaying a recording")

    op_set_sensitivity = op_set_hold = op_set_resolution = op_factory_reset = _read_only

    def run(self):
        while True:
            start, t0 = time.time(), self.rows[0]["t"]
            for row in self.rows:
                self.run_pending()
                time.sleep(max(0.0, (row["t"] - t0) - (time.time() - start)))
                self.hub.publish(row["frame"])
            self.hub.tracker = Tracker()  # don't let the loop's jump back to the start look like motion


def calibrate(hub, sensor, countdown, duration):
    """Record empty-room gate energies, then set each gate's thresholds just above that noise."""
    try:
        for left in range(countdown, 0, -1):
            hub.set_calib(phase="countdown", remaining=left)
            time.sleep(1)
        moves, statics = [], []
        end = time.time() + duration
        seen = -1
        while time.time() < end:
            hub.set_calib(phase="recording", remaining=math.ceil(end - time.time()), samples=len(moves))
            with hub.cond:
                hub.cond.wait_for(lambda: hub.frames_seen != seen, 0.5)
                seen, f = hub.frames_seen, hub.frame
            if f and f.get("gates_moving"):
                moves.append(f["gates_moving"])
                statics.append(f["gates_static"])
        if len(moves) < 20:
            raise RuntimeError("not enough engineering-mode frames to calibrate")

        def noise(rows):  # 2nd-highest value per gate: ignores a single glitch
            return [sorted(col)[-2] for col in zip(*rows)]

        n_move, n_static = noise(moves), noise(statics)
        old_static = hub.config.get("static_thresh") or L.DEFAULT_STATIC_THRESH
        move = [min(95, max(15, v + 12)) for v in n_move]
        # gates 0-1 have no stationary detection; keep whatever the sensor has there
        static = [old_static[g] if g < 2 else min(95, max(12, v + 10)) for g, v in enumerate(n_static)]
        hub.set_calib(phase="writing")
        sensor.do("set_sensitivity", move=move, static=static)
        hub.set_calib(phase="done", move=move, static=static, noise_move=n_move, noise_static=n_static,
                      samples=len(moves), at=time.time())
    except Exception as e:
        log.warning("calibration: %s", e)
        hub.set_calib(phase="error", error=str(e))


def make_handler(hub, sensor):
    calib_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def send_body(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send_body(json.dumps(obj).encode(), "application/json", code)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.send_body((WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.send_json(hub.snapshot())
            elif path == "/events":
                self.stream()
            else:
                self.send_body(b"not found", "text/plain", 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/api/calibrate":
                    countdown = max(0, min(60, int(body.get("countdown", 10))))
                    duration = max(5, min(60, int(body.get("duration", 15))))
                    with calib_lock:
                        if hub.calib.get("phase") in ("countdown", "recording", "writing"):
                            raise RuntimeError("calibration already running")
                        hub.set_calib(phase="countdown", remaining=countdown)
                        threading.Thread(target=calibrate, args=(hub, sensor, countdown, duration), daemon=True).start()
                elif path == "/api/hold":
                    sensor.do("set_hold", seconds=max(0, min(600, int(body["seconds"]))))
                elif path == "/api/resolution":
                    sensor.do("set_resolution", fine=bool(body["fine"]))
                elif path == "/api/thresholds":
                    sensor.do("set_sensitivity", move=body["move"], static=body["static"])
                elif path == "/api/factory-reset":
                    sensor.do("factory_reset")
                else:
                    return self.send_json({"ok": False, "error": "not found"}, 404)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            seq = -1
            try:
                while True:
                    # times out every 10 s so a quiet sensor still yields a keep-alive snapshot
                    seq, snap = hub.wait(seq, 10)
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="/dev/serial0", help="UART device (default /dev/serial0)")
    ap.add_argument("--baud", type=int, default=256000, help="sensor baud rate (factory default 256000)")
    ap.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default all interfaces)")
    ap.add_argument("--basic", action="store_true", help="don't switch the sensor into engineering mode")
    ap.add_argument("--demo", action="store_true", help="simulate a person instead of reading a sensor")
    ap.add_argument("--replay", metavar="FILE", help="play back a recording (JSON lines) instead of reading a sensor")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    hub = Hub()
    if args.replay:
        sensor = ReplaySensor(hub, args.replay)
    elif args.demo:
        sensor = DemoSensor(hub)
    else:
        sensor = SerialSensor(hub, args.serial, args.baud, not args.basic)
    threading.Thread(target=sensor.run, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(hub, sensor))
    server.daemon_threads = True
    log.info("Radar UI on http://%s:%d  (source: %s)", args.host, args.port, hub.status.get("source") or
             f"{args.serial} @ {args.baud}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
