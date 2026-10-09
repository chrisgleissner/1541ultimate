#!/usr/bin/env python3
"""E2E (experiment): fast forward, rewind and the video standard of the SID player.

Keys, injected into the C64 keyboard matrix over REST:

- `<-` (arrow left) or CRSR right, held: fast forward. The first 1.5 seconds
  run at 1 MHz, like the player always did; after that the CPU speed rises one
  turbo step every 0.4 seconds up to the machine's top speed (48 MHz on an
  Ultimate 64, 64 MHz on an Elite II or C64 Ultimate, 80 MHz on a C77).
- `,` (comma) or CRSR left (SHIFT + CRSR): rewind 10 seconds. Held, it rewinds
  again every second, by 10, 20, 30 and then 60 seconds at a time.

A tune cannot run backwards, so a rewind restarts the sub tune and fast
forwards it to the target second at full CPU speed. The firmware switches the
machine to the video standard the tune asks for, and enables the turbo
registers, while the player runs; both come back when the C64 leaves it.

What the suite measures, against the tune's own first 90 seconds recorded at
normal speed as the reference:

- the player's clock, read from screen RAM, for the position in the tune;
- the audio stream, for whether the tune plays and whether it plays from the
  right place. Each 20 ms of audio is reduced to a coarse log spectrum, and
  after every seek 8 seconds of it are matched against the reference: the
  best match within 1.5 s either way must lie within 0.15 s of the position
  the clock shows, and stand clear of the best match anywhere else. A
  loudness envelope alone cannot do this: this tune's beat repeats every
  0.4 s and its bars every 1.06 s, so loudness matches 0.5-0.65 a bar away;
  the spectrum matches 0.23 at most;
- the wall-clock time each seek and each fast forward section took.

The audio stream goes to the audio multicast group on a port of its own, so
no other listener on the bench receives it. Multicast also spares the device
the ARP lookup a unicast destination needs, which intermittently answers
"Network Host Resolve Error".

Needs an Ultimate 64 with the HVSC on its USB stick at HVSC_ROOT.
"""

from __future__ import annotations

import argparse
import itertools
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

# The one stanza that puts the shared library on sys.path; see tests/lib/bootstrap.py.
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                            if (p / "tests" / "lib").is_dir()) / "tests" / "lib"))
import bootstrap  # noqa: E402,F401

import cli                                            # noqa: E402
import streams                                        # noqa: E402
from api import UltimateApi                           # noqa: E402
from report import (Failure, check, detail,           # noqa: E402
                    format_exception, suite_fail, suite_ok, teardown_step)

SUITE = "sid_seek_test"

HVSC_ROOT = "/USB2/test-data/SID/HVSC/C64Music"
NTSC_TUNE = "MUSICIANS/P/Pegasus/DGN_Theme_remix.sid"   # NTSC only, 4:00
PAL_TUNE = "MUSICIANS/P/Peet/Rain.sid"                  # PAL only, 4:00

U64_STORE = "U64 Specific Settings"
SESSION_ITEMS = ("System Mode", "Turbo Control", "CPU Speed")
PAL_TIMED = {"PAL", "NTSC-50", "NTSC-50/L"}    # 63 cycles per line, 50 Hz
NTSC_TIMED = {"NTSC", "PAL-60", "PAL-60/L"}    # 65 cycles per line, 60 Hz
TURBO_REGISTERS = "U64 Turbo Registers"
STREAM_STORE = "Data Streams"
STREAM_ITEM = "Stream Audio to"

# The player's clock is "MM:SS" at the start of screen row 23.
CLOCK_OFFSET = 23 * 40
TITLE = "THE ULTIMATE C-64 SID PLAYER"

AUDIO_PACKET_BYTES = streams.AUDIO_PACKET_BYTES
FRAMES_PER_PACKET = (AUDIO_PACKET_BYTES - 2) // 4
BIN_PACKETS = 5                 # 20 ms bins, 960 samples
BIN_SECONDS = BIN_PACKETS * FRAMES_PER_PACKET / streams.RATE_NTSC_HZ
REFERENCE_SECONDS = 90
MATCH_SECONDS = 8.0             # audio compared after a seek
MATCH_SETTLE = 1.5              # skipped after landing, while the SID settles
MATCH_SEARCH = 1.5              # lag searched either way
MATCH_MIN_CORRELATION = 0.5
MATCH_MARGIN = 0.2              # over the best match away from the position
MATCH_MAX_LAG = 0.15
MATCH_AWAY = 0.1                # "away" starts this far from the best match
WRONG_PLACE = 5.0
AUDIBLE_RMS = 0.005


def local_address(host: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


class AudioRecorder:
    """Every audio packet from the device, timed by its sequence number."""

    def __init__(self, rate: float, interface: str) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind(("", 0))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(streams.AUDIO_GROUP) + socket.inet_aton(interface))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.packet_seconds = FRAMES_PER_PACKET / rate
        self.lock = threading.Lock()
        self.packets: list[tuple[float, float, np.ndarray]] = []    # (time, rms, left)
        self.lost = 0
        self.running = True
        self.anchor: tuple[float, int] | None = None
        self.last_seq: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        unwrapped = 0
        while self.running:
            try:
                data = self.sock.recv(2048)
            except OSError:
                continue
            now = time.monotonic()
            if len(data) != AUDIO_PACKET_BYTES:
                continue
            seq = struct.unpack_from("<H", data)[0]
            if self.last_seq is not None:
                step = (seq - self.last_seq) & 0xFFFF
                if step == 0 or step >= 0x8000:
                    continue
                self.lost += step - 1
                unwrapped += step
            self.last_seq = seq
            # The device's sample clock, free of network jitter; anchored again
            # when it drifts from arrival, as across a video standard switch.
            t = (self.anchor[0] + (unwrapped - self.anchor[1]) * self.packet_seconds
                 if self.anchor else now)
            if abs(now - t) > 0.1:
                self.anchor, t = (now, unwrapped), now
            if self.anchor is None:
                self.anchor = (now, unwrapped)
            left = np.frombuffer(data, dtype="<i2", offset=2)[0::2].astype(np.float32)
            rms = float(np.sqrt(np.mean(left * left))) / 32768.0
            with self.lock:
                self.packets.append((t, rms, left))

    def between(self, start: float, end: float) -> list[tuple[float, float, np.ndarray]]:
        with self.lock:
            return [p for p in self.packets if start <= p[0] < end]

    def close(self) -> None:
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()


def spectra(packets, start: float, seconds: float) -> np.ndarray:
    """Coarse log spectrum per 20 ms bin from `start`; a missing bin repeats the one before."""
    count = int(seconds / BIN_SECONDS)
    bins: list[list[np.ndarray]] = [[] for _ in range(count)]
    for t, _rms, left in packets:
        i = int((t - start) / BIN_SECONDS)
        if 0 <= i < count:
            bins[i].append(left)
    out = np.zeros((count, 50), dtype=np.float32)
    for i, parts in enumerate(bins):
        if parts:
            frame = np.concatenate(parts)
            out[i] = np.log1p(np.abs(np.fft.rfft(frame, BIN_PACKETS * FRAMES_PER_PACKET))[1:200:4])
        elif i:
            out[i] = out[i - 1]
    return out


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation with every band normalised over the window."""
    if len(a) != len(b) or len(a) < 10:
        return -1.0
    a = (a - a.mean(0)) / (a.std(0) + 1e-6)
    b = (b - b.mean(0)) / (b.std(0) + 1e-6)
    return float((a * b).mean())


class Player:
    def __init__(self, device: UltimateApi, recorder: AudioRecorder) -> None:
        self.device = device
        self.recorder = recorder
        self.screen = 0
        # wall time of tune second 0 in the reference run, and its spectra
        self.reference_start = 0.0
        self.reference: np.ndarray | None = None

    def play(self, rel: str) -> None:
        self.device.runners.sidplay(f"{HVSC_ROOT}/{rel}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.3)
            bank = 3 - (self.device.machine.readmem(0xDD00, 1)[0] & 0x03)
            matrix = self.device.machine.readmem(0xD018, 1)[0] >> 4
            screen = bank * 0x4000 + matrix * 0x400
            if TITLE in self.text(screen, 0, 40):
                self.screen = screen
                return
        raise Failure(f"the player screen did not appear for {rel}")

    def text(self, screen: int, offset: int, length: int) -> str:
        out = []
        for code in self.device.machine.readmem(screen + offset, length):
            code &= 0x7F
            out.append(chr(code + 64) if 1 <= code <= 26 else
                       chr(code) if 0x20 <= code <= 0x3F else ".")
        return "".join(out)

    def system_line(self) -> str:
        for row in range(6, 10):
            line = self.text(self.screen, row * 40, 40)
            if line.startswith("SYSTEM"):
                return line.strip()
        return ""

    def clock(self) -> int | None:
        s = self.text(self.screen, CLOCK_OFFSET, 5)
        try:
            return int(s[0:2]) * 60 + int(s[3:5])
        except ValueError:
            return None

    def wait_clock(self, predicate, timeout: float, poll: float = 0.03) -> tuple[float, int]:
        """Wall time at which the clock first satisfies `predicate`, and its value.

        The change happened between the start of the read before and the end of
        this one; the middle of that span keeps one slow REST read from moving
        the estimate by its whole duration.
        """
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            started = time.monotonic()
            value = self.clock()
            if value is not None and predicate(value):
                now = time.monotonic()
                return ((previous + now) / 2 if previous is not None else now), value
            previous = started
            time.sleep(poll)
        rasters = [self.device.machine.readmem(0xD012, 1)[0] for _ in range(3)]
        raise Failure(f"the clock did not get there within {timeout:.0f}s "
                      f"(it shows {self.clock()}; raster {rasters}, "
                      f"menu open {self.device.machine.menu_open()})")

    def key(self, transition: str, *names: str) -> None:
        self.device.machine.send_input(
            [{"kind": "keyboard", "inputs": list(names), "transition": transition}])

    def tune_second_at(self) -> tuple[float, int]:
        """Wall time of the next tick of the clock, and the second it ticks to."""
        now = self.clock()
        return self.wait_clock(lambda v: v != now, 3.0, 0.01)

    def check_position(self, label: str) -> None:
        """The audio after a seek is the reference audio at the clock's position."""
        tick, second = self.tune_second_at()
        start_second = second + MATCH_SETTLE
        wait_until = tick + MATCH_SETTLE + MATCH_SECONDS + 0.3
        time.sleep(max(0.0, wait_until - time.monotonic()))
        packets = self.recorder.between(tick + MATCH_SETTLE, wait_until)
        live = spectra(packets, tick + MATCH_SETTLE, MATCH_SECONDS)
        steps = int(MATCH_SEARCH / BIN_SECONDS)
        scores = [(self.reference_match(live, start_second + k * BIN_SECONDS), k * BIN_SECONDS)
                  for k in range(-steps, steps + 1)]
        best, best_lag = max(scores)
        away = max(r for r, lag in scores if abs(lag - best_lag) >= MATCH_AWAY)
        wrong = max(self.reference_match(live, start_second - WRONG_PLACE),
                    self.reference_match(live, start_second + WRONG_PLACE))
        loud = sum(p[1] for p in packets) / max(len(packets), 1)
        detail(f"{label}: audio at {start_second:.1f}s best matches the reference "
               f"{best_lag:+.2f}s away (r={best:.2f}); best elsewhere within "
               f"{MATCH_SEARCH:.1f}s r={away:.2f}; {WRONG_PLACE:.0f}s away r={wrong:.2f}; "
               f"mean rms {loud:.3f}")
        if loud < AUDIBLE_RMS:
            raise Failure(f"{label}: silent after the seek (mean rms {loud:.4f})")
        if (best < MATCH_MIN_CORRELATION or abs(best_lag) > MATCH_MAX_LAG
                or best - away < MATCH_MARGIN):
            raise Failure(f"{label}: the audio does not match the reference at the "
                          f"clock's position (lag {best_lag:+.2f}s, r={best:.2f}, "
                          f"elsewhere r={away:.2f})")

    def reference_match(self, live: np.ndarray, second: float) -> float:
        first = round(second / BIN_SECONDS)
        if self.reference is None or first < 0 or first + len(live) > len(self.reference):
            return -1.0
        return correlation(live, self.reference[first:first + len(live)])

    def check_normal_speed(self, label: str, seconds: float = 5.0) -> None:
        t0, c0 = self.tune_second_at()
        time.sleep(seconds)
        t1, c1 = self.tune_second_at()
        rate = (c1 - c0) / (t1 - t0)
        detail(f"{label}: clock runs at {rate:.2f} tune-s/s")
        if not 0.9 <= rate <= 1.1:
            raise Failure(f"{label}: clock runs at {rate:.2f} tune-s/s, not 1")


def session_settings(device: UltimateApi) -> dict[str, str]:
    return {item: device.configs.item(U64_STORE, item)["current"] for item in SESSION_ITEMS}


def rewind_tap(player: Player, label: str) -> int:
    """Tap `,` once; returns the landing second, checks the step and times the seek."""
    before = player.clock()
    pressed = time.monotonic()
    player.key("press", "comma")
    time.sleep(0.15)
    player.key("release", "comma")
    expected = {max(before - 10, 0), max(before + 1 - 10, 0)}
    landed_at, landed = player.wait_clock(lambda v: v in expected, 30.0)
    detail(f"{label}: {before}s -> {landed}s, seek took {landed_at - pressed:.2f}s")
    return landed


def run(args) -> None:
    device = UltimateApi(args.host, args.password or None, args.timeout)
    # A player left running holds the switched settings; leaving it restores
    # the user's own, which are the baseline.
    device.machine.reboot()
    time.sleep(2)
    original = session_settings(device)
    stream_dest = device.configs.item(STREAM_STORE, STREAM_ITEM)["current"]
    detail(f"settings before: {original}")
    recorder = AudioRecorder(streams.RATE_NTSC_HZ, local_address(args.host))
    player = Player(device, recorder)
    failures: list[Failure] = []

    def step(label, body):
        try:
            with check(label):
                body()
        except Failure as exc:
            failures.append(exc)

    try:
        device.streams.start("audio", ip=f"{streams.AUDIO_GROUP}:{recorder.port}")

        def video_standard():
            for rel, wanted, shown in ((PAL_TUNE, PAL_TIMED, "/ PAL"),
                                       (NTSC_TUNE, NTSC_TIMED, "/ NTSC")):
                player.play(rel)
                now = session_settings(device)
                line = player.system_line()
                detail(f"{rel}: {now}, screen {line!r}")
                if now["System Mode"] not in wanted or not line.endswith(shown):
                    raise Failure(f"{rel} plays on {now['System Mode']} ({line!r})")
                if now["Turbo Control"] != TURBO_REGISTERS:
                    raise Failure(f"Turbo Control is {now['Turbo Control']} while playing")
        step("the machine plays each tune on the video standard it asks for", video_standard)

        def reference():
            t1, _ = player.wait_clock(lambda v: v == 1, 5.0, 0.01)
            player.reference_start = t1 - 1.0
            t_end, _ = player.wait_clock(lambda v: v == REFERENCE_SECONDS, REFERENCE_SECONDS + 5,
                                         0.01)
            rate = (REFERENCE_SECONDS - 1) / (t_end - t1)
            packets = recorder.between(player.reference_start, t_end)
            loud = sum(p[1] for p in packets) / max(len(packets), 1)
            player.reference = spectra(packets, player.reference_start, REFERENCE_SECONDS)
            detail(f"reference: {len(packets)} audio packets, {recorder.lost} lost, "
                   f"mean rms {loud:.3f}, clock {rate:.3f} tune-s/s")
            if not 0.97 <= rate <= 1.03 or loud < AUDIBLE_RMS:
                raise Failure("the reference recording is not normal playback")
        step(f"the first {REFERENCE_SECONDS}s play at normal speed (reference)", reference)

        def rewind_once():
            # twice, so the audio after it is well inside the reference
            landed = rewind_tap(player, "comma tapped")
            player.wait_clock(lambda v: v == landed + 1, 3.0)
            rewind_tap(player, "comma tapped again")
            player.check_position("after one rewind")
            player.check_normal_speed("after one rewind")
        step("b) a tap of , rewinds 10 seconds and plays from there", rewind_once)

        def rewind_held(hold, expected, *keys):
            before = player.clock()
            pressed = time.monotonic()
            player.key("press", *keys)
            landings: list[tuple[float, int]] = []
            last = before
            while time.monotonic() - pressed < hold:
                value = player.clock()
                if value is not None and value not in (last, last + 1) and value != 0:
                    landings.append((time.monotonic() - pressed, value))
                if value is not None:
                    last = value
                time.sleep(0.02)
            player.key("release", *keys)
            steps = []
            prev = before
            for _at, value in landings:
                steps.append(prev - value)
                prev = value
            detail(f"{'+'.join(keys)} held {hold}s from {before}s: landed at "
                   + ", ".join(f"{v}s after {at:.2f}s" for at, v in landings)
                   + f"; steps {steps}")
            # each step after the first starts a second later in the tune
            if len(steps) != len(expected) or any(
                    not want - 1 <= got <= want for got, want in zip(steps, expected)):
                raise Failure(f"held rewind steps {steps}, expected about {expected}")
            player.check_position("after a held rewind")

        def forward_short(*keys):
            start = player.clock()
            pressed = time.monotonic()
            player.key("press", *keys)
            time.sleep(1.2)
            mid = player.clock()
            player.key("release", *keys)
            released = time.monotonic()
            rate = (mid - start) / (released - pressed)
            detail(f"{'+'.join(keys)} held 1.2s from {start}s: {mid}s, "
                   f"{rate:.1f} tune-s/s at 1 MHz")
            if not 3 <= rate <= 30:
                raise Failure(f"fast forward at 1 MHz runs at {rate:.1f} tune-s/s")
            player.check_position("after a short fast forward")
            player.check_normal_speed("after a short fast forward")
        # Each short fast forward follows a rewind that lands low enough for the
        # audio after it to lie inside the reference.
        step("b) CRSR left (SHIFT + CRSR) held rewinds again every second, by a growing step",
             lambda: rewind_held(2.9, [10, 20, 30], "left_shift", "cursor_left_right"))
        step("a) <- held briefly fast forwards at 1 MHz and plays from there",
             lambda: forward_short("arrow_left"))
        step("b) , held rewinds again every second, by a growing step",
             lambda: rewind_held(1.6, [10, 20], "comma"))
        step("a) CRSR right held briefly fast forwards at 1 MHz and plays from there",
             lambda: forward_short("cursor_left_right"))

        def forward_long():
            marks = {60: None, 120: None, 300: None, 600: None}
            samples = []
            start = player.clock()
            pressed = time.monotonic()
            player.key("press", "arrow_left")
            while time.monotonic() - pressed < 8.0:
                value = player.clock()
                at = time.monotonic() - pressed
                if value is not None:
                    samples.append((at, value))
                    for mark in marks:
                        if marks[mark] is None and value >= mark:
                            marks[mark] = at
                time.sleep(0.05)
            player.key("release", "arrow_left")
            rates = []
            for second in range(8):
                window = [s for s in samples if second <= s[0] < second + 1]
                if len(window) >= 2:
                    rates.append((window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0]))
            detail(f"<- held 8s from {start}s: tune-s/s per held second "
                   + ", ".join(f"{r:.0f}" for r in rates))
            detail("reached " + ", ".join(
                f"{m // 60}:{m % 60:02d} after {t:.2f}s" if t is not None else f"{m}s never"
                for m, t in marks.items()))
            if len(rates) < 8 or rates[-1] < 10 * rates[0]:
                raise Failure(f"the fast forward did not speed up: {rates}")
            if any(b < a * 0.85 for a, b in itertools.pairwise(rates[2:])):
                raise Failure(f"the fast forward slowed down while held: {rates}")
            if any(t is None for t in marks.values()):
                raise Failure(f"not every mark was reached: {marks}")
            player.check_normal_speed("after a long fast forward")
            tail = recorder.between(time.monotonic() - 2.0, time.monotonic())
            loud = sum(p[1] for p in tail) / max(len(tail), 1)
            detail(f"after a long fast forward: mean rms {loud:.3f}")
            if loud < AUDIBLE_RMS:
                raise Failure("silent after a long fast forward")
        step("c) <- held longer fast forwards faster and faster", forward_long)

        def rewind_far():
            landed = rewind_tap(player, "comma tapped far into the tune")
            if landed < 60:
                raise Failure(f"landed at {landed}s")
            player.check_normal_speed("after a long seek")
        step("b) a rewind far into the tune reaches its target at full CPU speed", rewind_far)

        def leave():
            device.machine.reset()
            time.sleep(3)
            now = session_settings(device)
            detail(f"settings after a reset: {now}")
            if now != original:
                raise Failure(f"the settings were not restored: {now} != {original}")
        step("a reset leaves the player and restores the settings", leave)

        if failures:
            raise failures[0]
    finally:
        teardown_step("stop the audio stream", lambda: device.streams.stop("audio"))
        teardown_step("restore the audio stream address",
                      lambda: device.configs.set(STREAM_STORE, STREAM_ITEM, stream_dest))
        recorder.close()
        teardown_step("leave the player", device.machine.reboot)
        for item, value in original.items():
            teardown_step(f"restore {item}",
                          lambda i=item, v=value: device.configs.set(U64_STORE, i, v))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check fast forward, rewind and the video standard switch of the "
                    "SID player on an Ultimate 64.")
    cli.add_device_arguments(parser)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:            # noqa: BLE001
        suite_fail(SUITE, format_exception(exc))
        return 1
    suite_ok(SUITE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
