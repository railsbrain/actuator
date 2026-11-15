import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from .pca9685 import open_device

I2C_BUS = int(os.environ.get("PWM_I2C_BUS", "0"))
I2C_ADDR = int(os.environ.get("PWM_I2C_ADDR", "0x40"), 0)
DEFAULT_FREQ = float(os.environ.get("PWM_FREQ", "50"))
MOCK = os.environ.get("PWM_MOCK") == "1"
DATA_DIR = Path(os.environ.get("PWM_DATA_DIR", Path(__file__).parent.parent / "data"))
CAL_FILE = DATA_DIR / "calibration.json"
WF_FILE = DATA_DIR / "workflows.json"
STATIC = Path(__file__).parent / "static"

lock = threading.Lock()
dev = None
# Last commanded duty per channel (0..1), 0 = off
state = {"duty": [0.0] * 16}


class Calibration(BaseModel):
    name: str = Field("", max_length=40)
    min_us: float = Field(500, ge=100, le=3000)
    center_us: float = Field(1500, ge=100, le=3000)
    max_us: float = Field(2500, ge=100, le=3000)
    invert: bool = False
    # Angle range in degrees: 0° = min_us, range_deg = max_us.
    range_deg: float = Field(180, ge=10, le=360)
    # Button-press actions (servo pushes a physical button): default hold times.
    short_press_ms: int = Field(300, ge=20, le=10_000)
    # Hard boot = long press (force power off), release, wait the gap, then a short press (power on).
    hard_boot_ms: int = Field(12_000, ge=1000, le=60_000)
    hard_boot_gap_ms: int = Field(1000, ge=0, le=60_000)

    @model_validator(mode="after")
    def ordered(self):
        if not self.min_us < self.max_us:
            raise ValueError("need min_us < max_us")
        # center_us is only used by the legacy `position` API; keep it inside the range.
        self.center_us = min(max(self.center_us, self.min_us), self.max_us)
        return self


calibration = [Calibration() for _ in range(16)]


def load_calibration():
    try:
        raw = json.loads(CAL_FILE.read_text())
        for i, item in enumerate(raw[:16]):
            calibration[i] = Calibration(**item)
    except FileNotFoundError:
        pass
    except Exception as e:  # corrupt file: keep defaults, don't crash the service
        print(f"[calibration] ignoring {CAL_FILE}: {e}")


def save_calibration():
    write_json(CAL_FILE, [c.model_dump() for c in calibration])


def position_to_us(cal: Calibration, pos: float) -> float:
    """pos -1..1: -1 = min_us, 0 = center_us, +1 = max_us (flipped when inverted)."""
    pos = max(-1.0, min(1.0, -pos if cal.invert else pos))
    span = (cal.max_us - cal.center_us) if pos >= 0 else (cal.center_us - cal.min_us)
    return cal.center_us + pos * span


def us_to_position(cal: Calibration, us: float) -> float:
    span = (cal.max_us - cal.center_us) if us >= cal.center_us else (cal.center_us - cal.min_us)
    pos = (us - cal.center_us) / span if span else 0.0
    pos = max(-1.0, min(1.0, pos))
    return -pos if cal.invert else pos


def angle_to_us(cal: Calibration, angle: float) -> float:
    """0° = min_us ... range_deg = max_us (reversed when inverted)."""
    f = max(0.0, min(1.0, angle / cal.range_deg))
    if cal.invert:
        f = 1 - f
    return cal.min_us + f * (cal.max_us - cal.min_us)


def us_to_angle(cal: Calibration, us: float) -> float:
    f = max(0.0, min(1.0, (us - cal.min_us) / (cal.max_us - cal.min_us)))
    if cal.invert:
        f = 1 - f
    return f * cal.range_deg


class Step(BaseModel):
    label: str = Field("", max_length=40)
    # channel -> angle in degrees (0 = calibrated min ... range_deg = calibrated max)
    angles: dict[str, float] | None = None
    # Legacy: calibrated position -1..1. Converted to `angles` when a workflow is loaded/saved.
    targets: dict[str, float] | None = None
    duration_ms: int = Field(1000, ge=0, le=60_000, description="time to move there")
    hold_ms: int = Field(0, ge=0, le=600_000, description="pause after arriving")

    @model_validator(mode="after")
    def check(self):
        given = self.angles or self.targets
        if not given:
            raise ValueError("step needs at least one channel")
        for k in given:
            if not (k.isdigit() and 0 <= int(k) <= 15):
                raise ValueError(f"bad channel '{k}'")
        if any(not 0 <= a <= 360 for a in (self.angles or {}).values()):
            raise ValueError("angle must be between 0 and 360")
        if any(not -1 <= p <= 1 for p in (self.targets or {}).values()):
            raise ValueError("position must be between -1 and 1")
        return self


def normalize(wf: "Workflow") -> "Workflow":
    """Make every step use `angles` (converting legacy `targets` with the current calibration)."""
    for st in wf.steps:
        if st.angles is None:
            st.angles = {
                k: round(us_to_angle(calibration[int(k)], position_to_us(calibration[int(k)], p)), 1)
                for k, p in st.targets.items()
            }
        st.targets = None
    return wf


class Workflow(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=100)
    repeat: int = Field(1, ge=0, le=10_000, description="rounds; 0 = loop until stopped")


NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")
RESERVED = {"status", "stop"}
workflows: dict[str, Workflow] = {}


def write_json(path: Path, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def load_workflows():
    try:
        for name, wf in json.loads(WF_FILE.read_text()).items():
            workflows[name] = normalize(Workflow(**wf))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[workflows] ignoring {WF_FILE}: {e}")


def save_workflows():
    write_json(WF_FILE, {n: w.model_dump(exclude_none=True) for n, w in workflows.items()})


def apply_us(ch: int, us: float) -> None:
    """Set a channel by pulse width. Caller holds `lock`."""
    duty = max(0.0, min(1.0, us * dev.freq / 1e6))
    dev.set_duty(ch, duty)
    state["duty"][ch] = duty


class Runner:
    """Runs one workflow at a time in a background thread."""

    TICK = 0.02  # 50 updates/s while moving

    def __init__(self):
        self.thread = None
        self.stop_evt = threading.Event()
        self.status = {"running": False, "name": None, "step": None, "label": None, "round": 0, "error": None}

    def start(self, name: str, wf: Workflow, only: int | None = None) -> None:
        if self.thread and self.thread.is_alive():
            raise HTTPException(409, "a workflow is already running")
        self.stop_evt.clear()
        self.status = {"running": True, "name": name, "step": None, "label": None, "round": 0, "error": None}
        self.thread = threading.Thread(target=self._run, args=(name, wf, only), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_evt.set()
        t = self.thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)

    def _move(self, step: Step) -> bool:
        """Ramp linearly to the step's targets. Returns True if stopped."""
        with lock:
            moves = []
            for k, angle in step.angles.items():
                ch = int(k)
                end = angle_to_us(calibration[ch], angle)
                d = state["duty"][ch]
                # A channel that is off has no position to ramp from: jump straight there.
                start = d / dev.freq * 1e6 if d > 0 else end
                moves.append((ch, start, end))
        dur = step.duration_ms / 1000
        t0 = time.monotonic()
        while True:
            frac = 1.0 if dur <= 0 else min(1.0, (time.monotonic() - t0) / dur)
            with lock:
                for ch, a, b in moves:
                    apply_us(ch, a + (b - a) * frac)
            if frac >= 1.0:
                return False
            if self.stop_evt.wait(self.TICK):
                return True

    def _run(self, name: str, wf: Workflow, only: int | None) -> None:
        try:
            steps = list(enumerate(wf.steps)) if only is None else [(only, wf.steps[only])]
            rounds = 0
            while True:
                rounds += 1
                self.status["round"] = rounds
                for i, step in steps:
                    self.status.update(step=i, label=step.label)
                    print(f"[workflow] {name} round {rounds} step {i + 1}/{len(wf.steps)} "
                          f"'{step.label}' -> {step.angles} deg over {step.duration_ms} ms", flush=True)
                    if self._move(step):
                        return
                    if only is None and step.hold_ms and self.stop_evt.wait(step.hold_ms / 1000):
                        return
                if only is not None or (wf.repeat and rounds >= wf.repeat):
                    return
        except Exception as e:  # e.g. I2C error: keep it visible instead of dying silently
            self.status["error"] = f"{type(e).__name__}: {e}"
            print(f"[workflow] {name} FAILED: {self.status['error']}", flush=True)
        finally:
            self.status = {**self.status, "running": False}
            print(f"[workflow] {name} ended (round {self.status['round']})", flush=True)


runner = Runner()


class PressJob(threading.Thread):
    """Push a physical button with a servo: go to 0° (press), hold, go to the range end (release).

    0° is min_us, or max_us when the channel is inverted, so "Invert direction" flips the press direction.
    `holds` is a list of hold times; between consecutive presses the job waits `gap_ms` (released).
    The servo then stays driven at the release position. One job per channel at a time."""

    def __init__(self, ch: int, action: str, holds: list[int], gap_ms: int = 0):
        super().__init__(daemon=True)
        cal = calibration[ch]
        self.ch, self.action, self.holds, self.gap_ms = ch, action, holds, gap_ms
        self.total_ms = sum(holds) + gap_ms * (len(holds) - 1)
        self.press_us = angle_to_us(cal, 0)
        self.release_us = angle_to_us(cal, cal.range_deg)
        self.cancel = threading.Event()
        self.phase = "starting"
        self.step = 0
        self.error = None

    def _move(self, us: float) -> bool:
        """Drive the servo; False if the job was cancelled (e.g. by All off) and nothing was moved."""
        with lock:
            if self.cancel.is_set():
                self.phase = "cancelled"
                return False
            apply_us(self.ch, us)
        return True

    def run(self):
        try:
            for n, hold in enumerate(self.holds, 1):
                self.step = n
                if not self._move(self.press_us):
                    return
                self.phase = "pressed"
                print(f"[press] CH{self.ch} {self.action} {n}/{len(self.holds)}: pressed at {self.press_us:.0f} us, holding {hold} ms", flush=True)
                self.cancel.wait(hold / 1000)
                if not self._move(self.release_us):
                    return
                print(f"[press] CH{self.ch} {self.action} {n}/{len(self.holds)}: released at {self.release_us:.0f} us", flush=True)
                self.phase = "released"
                if n < len(self.holds):
                    self.phase = "waiting"
                    self.cancel.wait(self.gap_ms / 1000)
            self.phase = "done"
        except Exception as e:  # e.g. I2C error
            self.phase, self.error = "error", f"{type(e).__name__}: {e}"
            print(f"[press] CH{self.ch} {self.action} FAILED: {self.error}", flush=True)
        finally:
            with presses_lock:
                if presses.get(self.ch) is self:
                    del presses[self.ch]


presses: dict[int, PressJob] = {}
presses_lock = threading.Lock()


def cancel_presses() -> None:
    with presses_lock:
        jobs = list(presses.values())
    for j in jobs:
        j.cancel.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dev
    load_calibration()
    load_workflows()
    dev = open_device(I2C_BUS, I2C_ADDR, DEFAULT_FREQ, MOCK)
    yield
    runner.stop()
    cancel_presses()
    dev.close()


app = FastAPI(title="Orange Pi PWM", lifespan=lifespan)


class ChannelCmd(BaseModel):
    # Provide exactly one of these.
    duty: float | None = Field(None, ge=0, le=1, description="0..1")
    pulse_us: float | None = Field(None, ge=0, le=20000, description="servo pulse width in µs")
    angle: float | None = Field(None, ge=0, le=360, description="degrees: 0=calibrated min ... range_deg=max")
    position: float | None = Field(None, ge=-1, le=1, description="legacy: -1=min, 0=center, 1=max")


class FreqCmd(BaseModel):
    hz: float = Field(ge=24, le=1526)


def snapshot():
    out = []
    for i, d in enumerate(state["duty"]):
        us = d / dev.freq * 1e6
        out.append({
            "ch": i, "duty": d, "pulse_us": us,
            "angle": round(us_to_angle(calibration[i], us), 1) if d > 0 else None,
            "position": us_to_position(calibration[i], us) if d > 0 else None,
        })
    return {"freq": dev.freq, "channels": out}


@app.get("/api/state")
def get_state():
    with lock:
        return snapshot()


@app.post("/api/channel/{ch}")
def set_channel(ch: int, cmd: ChannelCmd):
    if not 0 <= ch <= 15:
        raise HTTPException(404, "channel must be 0..15")
    given = [v for v in (cmd.duty, cmd.pulse_us, cmd.angle, cmd.position) if v is not None]
    if len(given) != 1:
        raise HTTPException(422, "provide exactly one of duty, pulse_us, angle or position")
    with lock:
        if cmd.duty is not None:
            duty = cmd.duty
        else:
            if cmd.pulse_us is not None:
                us = cmd.pulse_us
            elif cmd.angle is not None:
                us = angle_to_us(calibration[ch], cmd.angle)
            else:
                us = position_to_us(calibration[ch], cmd.position)
            duty = us * dev.freq / 1e6
        duty = max(0.0, min(1.0, duty))
        dev.set_duty(ch, duty)
        state["duty"][ch] = duty
        return snapshot()


@app.get("/api/calibration")
def get_calibration():
    return [c.model_dump() for c in calibration]


@app.put("/api/calibration/{ch}")
def put_calibration(ch: int, cal: Calibration):
    if not 0 <= ch <= 15:
        raise HTTPException(404, "channel must be 0..15")
    with lock:
        calibration[ch] = cal
        save_calibration()
        return [c.model_dump() for c in calibration]


@app.post("/api/frequency")
def set_frequency(cmd: FreqCmd):
    with lock:
        old = dev.freq
        dev.set_frequency(cmd.hz)
        # Keep pulse widths constant across a frequency change.
        for i, d in enumerate(state["duty"]):
            if d > 0:
                state["duty"][i] = min(1.0, d * dev.freq / old)
                dev.set_duty(i, state["duty"][i])
        return snapshot()


@app.get("/api/workflows")
def list_workflows():
    return {n: w.model_dump(exclude_none=True) for n, w in workflows.items()}


@app.get("/api/workflows/status")
def workflow_status():
    return runner.status


@app.post("/api/workflows/stop")
def stop_workflow():
    runner.stop()
    return runner.status


@app.put("/api/workflows/{name}")
def put_workflow(name: str, wf: Workflow):
    if not NAME_RE.match(name) or name in RESERVED:
        raise HTTPException(422, "name: 1-40 chars of letters, digits, space, _ or -")
    with lock:
        workflows[name] = normalize(wf)
        save_workflows()
    return list_workflows()


@app.delete("/api/workflows/{name}")
def delete_workflow(name: str):
    if name not in workflows:
        raise HTTPException(404, "no such workflow")
    with lock:
        del workflows[name]
        save_workflows()
    return list_workflows()


@app.post("/api/workflows/{name}/run")
def run_workflow(name: str, step: int | None = None):
    """Run a workflow; with ?step=N run just that one step (0-based)."""
    wf = workflows.get(name)
    if wf is None:
        raise HTTPException(404, "no such workflow")
    if step is not None and not 0 <= step < len(wf.steps):
        raise HTTPException(404, "no such step")
    runner.start(name, wf, step)
    return runner.status


def run_press(ch: int, action: str, holds: list[int], gap_ms: int, wait: bool):
    if not 0 <= ch <= 15:
        raise HTTPException(404, "channel must be 0..15")
    job = PressJob(ch, action, holds, gap_ms)
    with presses_lock:
        if ch in presses:
            raise HTTPException(409, f"channel {ch} is already pressing ({presses[ch].action})")
        presses[ch] = job
    job.start()
    if wait:
        job.join(timeout=job.total_ms / 1000 + 5)
    if job.error:
        raise HTTPException(500, job.error)
    return {
        "channel": ch, "action": action, "holds_ms": holds, "gap_ms": gap_ms, "total_ms": job.total_ms,
        "press_us": job.press_us, "release_us": job.release_us,
        "state": job.phase if wait else "started",
    }


@app.post("/api/channel/{ch}/press")
def press(ch: int, hold_ms: int = Query(300, ge=20, le=60_000), wait: bool = False):
    """Press (go to 0°: min_us, or max_us if inverted), hold for hold_ms, release. wait=true blocks until released."""
    return run_press(ch, "press", [hold_ms], 0, wait)


@app.post("/api/channel/{ch}/power")
def power(ch: int, hold_ms: int | None = Query(None, ge=20, le=60_000), wait: bool = False):
    """Short press: power on/off. Duration = the channel's short_press_ms unless hold_ms is given."""
    if not 0 <= ch <= 15:
        raise HTTPException(404, "channel must be 0..15")
    return run_press(ch, "power", [hold_ms or calibration[ch].short_press_ms], 0, wait)


@app.post("/api/channel/{ch}/hard-boot")
def hard_boot(
    ch: int,
    hold_ms: int | None = Query(None, ge=20, le=60_000, description="long press (force off); default hard_boot_ms"),
    gap_ms: int | None = Query(None, ge=0, le=60_000, description="pause after releasing; default hard_boot_gap_ms"),
    power_ms: int | None = Query(None, ge=20, le=60_000, description="final short press (power on); default short_press_ms"),
    wait: bool = False,
):
    """Long press (default 12 s), release, wait the gap, then the short power press to switch the machine on."""
    if not 0 <= ch <= 15:
        raise HTTPException(404, "channel must be 0..15")
    cal = calibration[ch]
    holds = [hold_ms or cal.hard_boot_ms, power_ms or cal.short_press_ms]
    return run_press(ch, "hard-boot", holds, cal.hard_boot_gap_ms if gap_ms is None else gap_ms, wait)


@app.get("/api/presses")
def active_presses():
    with presses_lock:
        return {
            str(c): {"action": j.action, "phase": j.phase, "press": f"{j.step}/{len(j.holds)}", "total_ms": j.total_ms}
            for c, j in presses.items()
        }


@app.post("/api/off")
def all_off():
    runner.stop()  # before taking the lock: the runner thread needs it to finish
    cancel_presses()
    with lock:
        dev.all_off()
        state["duty"] = [0.0] * 16
        return snapshot()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
