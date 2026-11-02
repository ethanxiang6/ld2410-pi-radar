"""HLK-LD2410 / LD2410B / LD2410C serial protocol: frame parser and config commands.

The sensor streams "report" frames continuously (about 10/s):

    F4 F3 F2 F1 | len (2, LE) | type | AA | target data ... | 55 | 00 | F8 F7 F6 F5

type 0x02 = basic mode, 0x01 = engineering mode (adds per-gate energies).
Command / ACK frames use the FD FC FB FA ... 04 03 02 01 envelope instead. Commands must be
wrapped in ENABLE_CONFIG ... END_CONFIG; each is answered by an ACK whose word is cmd | 0x0100.
"""
import struct

DATA_HEAD = b"\xF4\xF3\xF2\xF1"
DATA_TAIL = b"\xF8\xF7\xF6\xF5"
CMD_HEAD = b"\xFD\xFC\xFB\xFA"
CMD_TAIL = b"\x04\x03\x02\x01"

GATE_METERS = 0.75  # default gate size; 0.2 m in fine-resolution ("desk") mode
MAX_PAYLOAD = 64    # real frames are < 50 bytes; anything bigger is a false header

STATE_NAMES = {0: "none", 1: "moving", 2: "stationary", 3: "both"}

# Factory defaults (gates 0..8), used by demo mode and as a fallback.
DEFAULT_MOVE_THRESH = [50, 50, 40, 30, 20, 15, 15, 15, 15]
DEFAULT_STATIC_THRESH = [0, 0, 40, 40, 30, 30, 20, 20, 20]


def command(word, value=b""):
    body = struct.pack("<H", word) + value
    return CMD_HEAD + struct.pack("<H", len(body)) + body + CMD_TAIL


def command_word(frame):
    return struct.unpack_from("<H", frame, 6)[0]


ENABLE_CONFIG = command(0x00FF, b"\x01\x00")
END_CONFIG = command(0x00FE)
ENGINEERING_ON = command(0x0062)
ENGINEERING_OFF = command(0x0063)
READ_PARAMS = command(0x0061)
READ_FIRMWARE = command(0x00A0)
READ_RESOLUTION = command(0x00AB)
FACTORY_RESET = command(0x00A2)  # takes effect after RESTART
RESTART = command(0x00A3)        # module reboots right after the ACK


def set_max_gates(move_gate, static_gate, hold_s):
    """Farthest moving / stationary gate to report, and seconds to hold "present" after the last detection."""
    return command(0x0060, struct.pack("<HIHIHI", 0, move_gate, 1, static_gate, 2, hold_s))


def set_sensitivity(gate, move, static):
    """Per-gate trigger thresholds (0-100): energy above them counts as a detection."""
    return command(0x0064, struct.pack("<HIHIHI", 0, gate, 1, move, 2, static))


def set_resolution(fine):
    """0.2 m gates (fine) or 0.75 m gates. Takes effect after RESTART; needs firmware >= 2.44."""
    return command(0x00AA, struct.pack("<H", 1 if fine else 0))


def parse_ack(p):
    if len(p) < 4:
        return None
    word, status = struct.unpack_from("<HH", p, 0)
    return {"cmd": word & ~0x0100, "ok": status == 0, "data": p[4:]}


def parse_params(d):
    if len(d) < 4 or d[0] != 0xAA:
        return {}
    n = d[1]
    move = list(d[4:4 + n + 1])
    static = list(d[5 + n:5 + 2 * n + 1])
    hold = struct.unpack_from("<H", d, 5 + 2 * n + 1)[0] if len(d) >= 5 + 2 * n + 3 else None
    return {"max_gate": n, "max_move_gate": d[2], "max_static_gate": d[3],
            "move_thresh": move, "static_thresh": static, "hold_s": hold}


def parse_firmware(d):
    if len(d) < 8:
        return None
    _ftype, major, minor = struct.unpack_from("<HHI", d, 0)
    return f"V{major >> 8}.{major & 0xFF:02x}.{minor:08x}"


def parse_resolution(d):
    return 0.2 if len(d) >= 2 and struct.unpack_from("<H", d, 0)[0] == 1 else 0.75


def parse_report(p):
    """Decode the payload of a report frame into a dict, or None if malformed."""
    if len(p) < 13 or p[1] != 0xAA:
        return None
    ftype = p[0]
    state, mov_cm, mov_e, sta_cm, sta_e, det_cm = struct.unpack_from("<BHBHBH", p, 2)
    frame = {
        "mode": "engineering" if ftype == 0x01 else "basic",
        "state": state,
        "state_name": STATE_NAMES.get(state, "unknown"),
        "moving": {"distance_cm": mov_cm, "energy": mov_e, "active": bool(state & 1)},
        "stationary": {"distance_cm": sta_cm, "energy": sta_e, "active": bool(state & 2)},
        "detect_cm": det_cm,
        "gates_moving": None,
        "gates_static": None,
        "light": None,
        "out": None,
    }
    if ftype == 0x01 and len(p) > 13:
        n_mov, n_sta = p[11], p[12]
        i = 13
        if i + n_mov + 1 + n_sta + 1 <= len(p):
            frame["gates_moving"] = list(p[i:i + n_mov + 1])
            i += n_mov + 1
            frame["gates_static"] = list(p[i:i + n_sta + 1])
            i += n_sta + 1
            # LD2410B/C append light-sensor value and OUT pin state before 55 00
            if i + 2 <= len(p) - 2:
                frame["light"] = p[i]
                frame["out"] = p[i + 1]
    return frame


class Parser:
    """Incremental byte-stream parser. feed() returns ("data", frame) and ("ack", ack) tuples."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            starts = [i for i in (self.buf.find(DATA_HEAD), self.buf.find(CMD_HEAD)) if i >= 0]
            if not starts:
                del self.buf[:-3]  # keep a possible partial header
                return out
            del self.buf[:min(starts)]
            if len(self.buf) < 6:
                return out
            is_data = self.buf.startswith(DATA_HEAD)
            length = struct.unpack_from("<H", self.buf, 4)[0]
            if length > MAX_PAYLOAD:
                del self.buf[:1]
                continue
            total = 4 + 2 + length + 4
            if len(self.buf) < total:
                return out
            tail = bytes(self.buf[total - 4:total])
            if tail != (DATA_TAIL if is_data else CMD_TAIL):
                del self.buf[:1]  # resync
                continue
            payload = bytes(self.buf[6:6 + length])
            del self.buf[:total]
            item = parse_report(payload) if is_data else parse_ack(payload)
            if item:
                out.append(("data" if is_data else "ack", item))
