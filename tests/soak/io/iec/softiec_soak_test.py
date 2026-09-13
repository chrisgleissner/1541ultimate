#!/usr/bin/env python3
"""Soak: the Software IEC drive under the use a C64 OS session puts it through.

GideonZ/1541ultimate#877 reports the whole firmware becoming unresponsive while C64
OS starts from a Software IEC partition, at a different point each time. This suite
repeats that kind of session on a real device for a fixed time and fails if the
firmware stops answering, if the drive gives back data that differs from what was
stored, or if the heap keeps shrinking.

The C64 runs the IEC agent (tests/e2e/io/iec/iec_agent.asm), so every byte on the bus
is one the KERNAL puts there. Each iteration is one of:

  * the boot pattern from the trace attached to #877: the binary Change Partition,
    CD to absolute paths, the four M-R probes, UI, a scratch with a path, a listing
    abandoned after five bytes, a settings file held open on one channel while a
    library's first two bytes are read on another and the whole library again on a
    third, an open of a file that is not there, and a directory filtered by pattern;
  * a sequential file saved with @, read back and compared, and scratched;
  * a relative file written record by record, positioned and read back;
  * direct access to a disk image: U2, U1 and B-P on a buffer channel;
  * directories made, entered and removed; a rename and a copy;
  * listings of every kind, some abandoned part way, some on a data channel;
  * the other commands a program uses: G-P, T-RI, I, UJ, M-R, and a few unknown
    commands, which must answer and change nothing.

While the C64 works, two more lanes load the device the way a user at a PC would:
REST reads the drive list, which takes the drive's lock, and the heap figure, and FTP
lists the soak directory, reads a file the C64 is also reading and writes and deletes
one of its own. None of them touches C64 memory, which would disturb a transfer.

The firmware is declared dead when a transaction does not finish and REST does not
answer either. The failure names the iteration and the last operations.

Profiles: `stress` runs for ten minutes, `soak` for four hours. `--duration` overrides.
`--no-lanes` leaves out the REST and FTP lanes, to tell the drive's heap use from theirs.
The suite needs a C64 with a standard KERNAL, REST and FTP, and one Software IEC
partition numbered 1. It uses device 11, and restores the settings, the partition's
working directory, and removes its own directory.
"""
import argparse
import random
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents
                          if (p / "tests" / "lib").is_dir()) / "tests" / "lib"))
import bootstrap  # noqa: E402,F401
import cli  # noqa: E402
import ftp  # noqa: E402
from api import UltimateApi  # noqa: E402
from config_snapshot import Snapshot  # noqa: E402
from report import Failure, check, detail, section, suite_fail, suite_ok, teardown_step  # noqa: E402

sys.path.insert(0, bootstrap.directory("e2e", "io", "iec"))
from iec_agent import CLOSE, OPEN, READ_COUNT, READ_TO_EOI, STATUS_BYTES, WRITE, Agent  # noqa: E402

SUITE = "softiec_soak_test"
PROFILES = {"stress": 10 * 60, "soak": 4 * 60 * 60}
# The heap may move while caches fill; what is not allowed is a steady loss. The
# measurement starts after the warm-up iterations and allows this much per iteration.
WARMUP_ITERATIONS = 5
LEAK_TOLERANCE_BYTES_PER_ITERATION = 256
REST_LANE_SECONDS = 1.5
FTP_LANE_SECONDS = 4.0
# How many operations the failure report names.
HISTORY = 25


def pattern(name, size):
    """The content of a fixture file: deterministic, different for each file."""
    seed = sum(name.encode("ascii"))
    return bytes(((i * 31 + seed) & 0xFF) for i in range(size))


# The files C64 OS reads at boot, by the paths it uses, with their sizes from the trace.
FIXTURE = {
    "OS/SETTINGS/SYSTEM.T.seq": 400,
    "OS/SETTINGS/CONFIG.T.seq": 13,
    "OS/SETTINGS/COMPONENTS.T.seq": 683,
    "OS/LIBRARY/REDIRECT.O.prg": 18,
    "OS/LIBRARY/WORKSPACE.O.prg": 373,
    "OS/LIBRARY/IEC.LIB.R.prg": 624,
    "OS/KERNAL/MENU.O.prg": 2209,
    "OS/KERNAL/FILE.O.prg": 1338,
    "OS/DRIVERS/KBD.C64.prg": 567,
    "OS/TK/TKVIEW.O.prg": 1694,
}
FIXTURE_DIRS = ("OS", "OS/SETTINGS", "OS/LIBRARY", "OS/KERNAL", "OS/DRIVERS", "OS/TK",
                "OS/TEMPORARY", "WORK", "PCLANE")


class Dead(Failure):
    """The firmware stopped answering."""


class Session:
    """One soak run: the device, the agent, the fixture, and what happened."""

    def __init__(self, args):
        self.args = args
        self.api = UltimateApi(args.host, args.password, args.timeout)
        self.agent = Agent(self.api)
        self.random = random.Random(args.seed)
        self.folder = "soak" + uuid.uuid4().hex[:6]
        self.here = self.folder.upper()
        self.root = None
        self.history = []
        self.iteration = 0
        self.counts = {}
        self.lane_errors = []
        self.lane_counts = {"rest": 0, "ftp": 0}
        self.stop = threading.Event()

    # -- the C64 lane ----------------------------------------------------------------

    def note(self, what):
        self.history.append(f"{self.iteration}: {what}")
        del self.history[:-HISTORY]

    def command(self, text, allowed=None):
        data = text if isinstance(text, bytes) else text.encode("latin-1")
        self.note(f"command {data!r}")
        self.agent.call(WRITE, 15, data)
        response = self.agent.call(READ_TO_EOI, 15, expect=STATUS_BYTES).decode("latin-1").strip()
        code = response[:2]
        if not code.isdigit():
            raise Failure(f"{data!r} answered {response!r}, which is not a status")
        if allowed is not None and int(code) not in allowed:
            raise Failure(f"{data!r} answered {response!r}")
        return response

    def status(self):
        return self.agent.call(READ_TO_EOI, 15, expect=STATUS_BYTES).decode("latin-1").strip()

    def open(self, channel, name, secondary=None):
        data = name if isinstance(name, bytes) else name.encode("latin-1")
        self.note(f"open {channel} {data!r}")
        self.agent.call(OPEN, channel=channel, data=data, secondary=secondary)
        return self.status()

    def close(self, channel):
        self.note(f"close {channel}")
        self.agent.call(CLOSE, channel=channel)

    def read(self, channel, limit=None):
        """Bytes from a channel up to its end, or at most `limit` of them."""
        out = b""
        while limit is None or len(out) < limit:
            want = 254 if limit is None else min(254, limit - len(out))
            block = self.agent.call(READ_COUNT, channel, count=want)
            out += block
            if len(block) < want or (self.agent.last_status & 64):
                break
        return out

    def read_file(self, channel, name):
        response = self.open(channel, name)
        try:
            if not response.startswith("00"):
                return response, None
            return response, self.read(channel)
        finally:
            self.close(channel)

    def expect_file(self, channel, name, fixture):
        response, data = self.read_file(channel, name)
        if data is None:
            raise Failure(f"{name} did not open: {response}")
        want = pattern(fixture, FIXTURE[fixture])
        if data != want:
            raise Failure(f"{name} read {len(data)} bytes that differ from the {len(want)} stored")

    def partition(self):
        self.command(bytes([ord("C"), 0xD0, 1]), allowed=(2,))

    def boot(self):
        """The order of operations C64 OS follows at boot, from the #877 trace."""
        here = self.here
        self.partition()
        self.command(f"CD//{here}/OS\r", allowed=(0,))
        for low, high in ((0xA4, 0xFE), (0xC5, 0xE5), (0xE8, 0xA6), (0x02, 0x00)):
            self.agent.call(WRITE, 15, bytes([ord("M"), ord("-"), ord("R"), low, high, 2]))
            reply = self.agent.call(READ_TO_EOI, 15, expect=2)
            self.note(f"M-R answered {reply!r}")
        self.command("UI", allowed=(73,))
        self.command("S/TEMPORARY/:*", allowed=(1,))
        self.open(3, "$", secondary=0)
        self.read(3, limit=5)
        self.close(3)
        held = self.open(2, "/SETTINGS/:SYSTEM.T")
        if not held.startswith("00"):
            raise Failure(f"/SETTINGS/:SYSTEM.T did not open: {held}")
        first = self.read(2, limit=40)
        for name, fixture in (("/LIBRARY/:WORKSPACE.O", "OS/LIBRARY/WORKSPACE.O.prg"),
                              ("/LIBRARY/:IEC.LIB.R", "OS/LIBRARY/IEC.LIB.R.prg"),
                              ("/KERNAL/:MENU.O", "OS/KERNAL/MENU.O.prg")):
            self.open(4, name)
            header = self.read(4, limit=2)
            self.close(4)
            if header != pattern(fixture, 2):
                raise Failure(f"the first two bytes of {name} read {header!r}")
            self.expect_file(5, name, fixture)
        missing = self.open(6, "/TEMPORARY/:UPDATER")
        self.close(6)
        if not missing.startswith("62"):
            raise Failure(f"a file that is not there answered {missing}")
        self.expect_file(14, "/SETTINGS/:CONFIG.T", "OS/SETTINGS/CONFIG.T.seq")
        self.command(bytes([ord("C"), 0xD0, 1]), allowed=(2,))
        self.command(f"CD//{here}/OS/DRIVERS/", allowed=(0,))
        self.expect_file(5, "KBD.C64", "OS/DRIVERS/KBD.C64.prg")
        self.command(f"CD//{here}/OS/SETTINGS/", allowed=(0,))
        self.open(3, "$:CONFIG*", secondary=0)
        listing = self.read(3)
        self.close(3)
        self.command(f"CD//{here}/OS", allowed=(0,))
        if b"CONFIG.T" not in listing:
            raise Failure(f"$:CONFIG* in SETTINGS did not list CONFIG.T: {listing!r}")
        rest = self.read(2)
        self.close(2)
        if first + rest != pattern("OS/SETTINGS/SYSTEM.T.seq", 400):
            raise Failure("the settings file held open during the boot read back changed")

    def save_and_scratch(self):
        size = self.random.randrange(1, 1500)
        name = f"F{self.random.randrange(4)}"
        data = bytes(self.random.randrange(256) for _ in range(size))
        self.partition()
        self.command(f"CD//{self.here}/WORK/", allowed=(0,))
        response = self.open(7, f"@:{name},S,W")
        if not response.startswith("00"):
            raise Failure(f"@:{name},S,W answered {response}")
        for start in range(0, size, 254):
            self.agent.call(WRITE, 7, data[start:start + 254])
        self.close(7)
        response, back = self.read_file(8, f"{name},S")
        if back != data:
            raise Failure(f"{name} saved {size} bytes and read back {len(back or b'')} that differ ({response})")
        if self.random.randrange(3) == 0:
            self.command(f"S:{name}", allowed=(1,))

    def relative(self):
        length = self.random.randrange(2, 120)
        self.partition()
        self.command(f"CD//{self.here}/WORK/", allowed=(0,))
        name = f"R{length}"
        self.command(f"S:{name}", allowed=(1,))
        self.open(9, name.encode("ascii") + b",L," + bytes([length]))
        records = {}
        for _ in range(self.random.randrange(1, 5)):
            record = self.random.randrange(1, 40)
            payload = bytes(self.random.randrange(1, 256) for _ in range(self.random.randrange(1, length)))
            self.command(bytes([ord("P"), 0x60 | 9, record & 0xFF, record >> 8, 1]), allowed=(0, 50))
            self.agent.call(WRITE, 9, payload)
            records[record] = payload
        for record, payload in records.items():
            self.command(bytes([ord("P"), 0x60 | 9, record & 0xFF, record >> 8, 1]), allowed=(0,))
            back = self.read(9, limit=length)
            if back[:len(payload)] != payload:
                raise Failure(f"record {record} of {name} read {back!r}, wrote {payload!r}")
        self.close(9)

    def direct_access(self):
        self.partition()
        self.command(f"CD//{self.here}/DISK.D64", allowed=(0,))
        self.open(10, "#")
        data = bytes(self.random.randrange(256) for _ in range(256))
        sector = self.random.randrange(0, 21)
        self.command("B-P 10 0", allowed=(0,))
        self.agent.call(WRITE, 10, data[:254])
        self.agent.call(WRITE, 10, data[254:])
        self.command(f"U2:10,0,1,{sector}", allowed=(0,))
        self.command(f"U1:10,0,1,{sector}", allowed=(0,))
        self.command("B-P 10 0", allowed=(0,))
        back = self.read(10, limit=256)
        self.close(10)
        self.command(f"CD//{self.here}", allowed=(0,))
        if back != data:
            raise Failure(f"sector 1/{sector} read back differs after U2 and U1")

    def directories(self):
        self.partition()
        self.command(f"CD//{self.here}/WORK/", allowed=(0,))
        name = f"D{self.random.randrange(3)}"
        self.command(f"MD:{name}", allowed=(0, 63))
        self.command(f"CD:{name}", allowed=(0,))
        self.command("CD_", allowed=(0,))
        self.command(f"C:COPY=//{self.here}/OS/SETTINGS/:CONFIG.T", allowed=(0, 62, 63))
        self.command("R:MOVED=COPY", allowed=(0, 62, 63))
        self.command("S:MOVED", allowed=(1,))
        self.command(f"RD:{name}", allowed=(0, 62))

    def listings(self):
        self.partition()
        name = self.random.choice([f"$//{self.here}/OS/", "$=P", f"$//{self.here}/:*=S",
                                   f"$//{self.here}/OS/KERNAL/:*=L", "$//", f"$//{self.here}/NOSUCH/"])
        secondary = 0 if self.random.randrange(4) else 3
        response = self.open(11, name, secondary=secondary)
        if response.startswith("00"):
            self.read(11, limit=None if self.random.randrange(2) else self.random.randrange(1, 60))
        self.close(11)

    def other_commands(self):
        for text, allowed in (("G-P", None), ("T-RI", None), ("I", (0,)), ("UJ", (73,)),
                              ("Z9", (31,)), ("E", (30,)), ("XYZ", (30,)), ("V", (31,))):
            if self.random.randrange(2):
                continue
            if text in ("G-P", "T-RI"):
                self.agent.call(WRITE, 15, text.encode("ascii"))
                self.note(f"{text} answered {self.agent.call(READ_TO_EOI, 15, expect=40)!r}")
            else:
                self.command(text, allowed=allowed)

    OPERATIONS = (("boot", boot, 4), ("save", save_and_scratch, 2), ("relative", relative, 1),
                  ("direct access", direct_access, 1), ("directories", directories, 1),
                  ("listings", listings, 2), ("other commands", other_commands, 1))

    def choose(self):
        total = sum(weight for _, _, weight in self.OPERATIONS)
        pick = self.random.randrange(total)
        for label, action, weight in self.OPERATIONS:
            if pick < weight:
                return label, action
            pick -= weight
        raise AssertionError("unreachable")

    # -- the PC lanes ----------------------------------------------------------------

    def rest_lane(self):
        # A client of its own: the agent's lane must not share a connection with this one.
        api = UltimateApi(self.args.host, self.args.password, self.args.timeout)
        while not self.stop.wait(REST_LANE_SECONDS):
            try:
                api.rest.json("/v1/drives")
                api.machine.heap_free()
                self.lane_counts["rest"] += 1
            except Exception as exc:  # the main lane decides whether this is a death
                self.lane_errors.append(f"rest at iteration {self.iteration}: {exc}")

    def ftp_lane(self):
        directory = f"{self.root.rstrip('/')}/{self.folder}"
        payload = pattern("pclane", 3000)
        while not self.stop.wait(FTP_LANE_SECONDS):
            try:
                with ftp.session(self.args.host, self.args.password, timeout=20) as client:
                    ftp.names(client, f"{directory}/OS/KERNAL")
                    data = ftp.retrieve(client, f"{directory}/OS/KERNAL/MENU.O.prg")
                    if data != pattern("OS/KERNAL/MENU.O.prg", 2209):
                        self.lane_errors.append(f"ftp at iteration {self.iteration}: MENU.O.prg differs")
                    ftp.store(client, f"{directory}/PCLANE/PC.BIN", payload)
                    client.delete(f"{directory}/PCLANE/PC.BIN")
                self.lane_counts["ftp"] += 1
            except Exception as exc:
                self.lane_errors.append(f"ftp at iteration {self.iteration}: {exc}")

    # -- the run ---------------------------------------------------------------------

    def alive(self):
        try:
            self.api.rest.json("/v1/info")
            return True
        except Exception:
            return False

    def fixture(self):
        directory = f"{self.root.rstrip('/')}/{self.folder}"
        with ftp.session(self.args.host, self.args.password) as client:
            client.mkd(directory)
            for sub in FIXTURE_DIRS:
                client.mkd(f"{directory}/{sub}")
            for name, size in FIXTURE.items():
                ftp.store(client, f"{directory}/{name}", pattern(name, size))
        self.api.files.create_d64(f"{directory}/DISK.D64", diskname="SOAK")

    def run(self, duration):
        heap = []
        deadline = time.monotonic() + duration
        lanes = [] if self.args.no_lanes else [threading.Thread(target=self.rest_lane, daemon=True),
                                               threading.Thread(target=self.ftp_lane, daemon=True)]
        for lane in lanes:
            lane.start()
        failures = []
        try:
            while time.monotonic() < deadline:
                self.iteration += 1
                label, action = self.choose()
                self.counts[label] = self.counts.get(label, 0) + 1
                self.note(f"begin {label}")
                try:
                    action(self)
                except Failure as exc:
                    if not self.alive():
                        raise Dead(f"the firmware stopped answering during iteration {self.iteration} "
                                   f"({label}): {exc}") from exc
                    failures.append(f"iteration {self.iteration} ({label}): {exc}")
                    try:
                        self.recover()
                    except Failure as stuck:
                        raise Dead(f"the drive stopped answering the C64 during iteration {self.iteration} "
                                   f"({label}) while REST still answers: {exc}; then {stuck}") from stuck
                    if len(failures) > 5:
                        break
                if self.iteration >= WARMUP_ITERATIONS:
                    heap.append(self.api.machine.heap_free())
                if self.iteration % 10 == 0:
                    detail(f"{self.iteration} iterations, {dict(sorted(self.counts.items()))}, "
                           f"heap {heap[-1] if heap else '-'}, lanes {self.lane_counts}")
        finally:
            self.stop.set()
            for lane in lanes:
                lane.join(timeout=30)
        return heap, failures

    def recover(self):
        """Close what an interrupted operation left open, so the next one starts clean."""
        for channel in range(2, 15):
            try:
                self.agent.call(CLOSE, channel=channel)
            except Failure:
                pass
        try:
            self.agent.call(CLOSE, channel=15)
            self.agent.call(OPEN, channel=15)
            self.status()
        except Failure:
            self.agent.start()
            self.agent.call(OPEN, channel=15)
            self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_device_arguments(parser)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="stress")
    parser.add_argument("--duration", type=float, help="seconds, instead of the profile's")
    parser.add_argument("--seed", type=int, default=877)
    parser.add_argument("--no-lanes", action="store_true",
                        help="only the C64's operations, to tell the drive's heap use from REST's and FTP's")
    args = parser.parse_args()
    duration = args.duration if args.duration else PROFILES[args.profile]
    session = Session(args)
    api = session.api
    try:
        section("setup")
        with check("the device has one Software IEC partition and device 11 is free"):
            drives = {name: value for entry in api.rest.json("/v1/drives")["drives"] for name, value in entry.items()}
            if any(d.get("enabled") and d.get("bus_id") == 11 for name, d in drives.items()
                   if name not in ("a", "IEC Drive")):
                raise Failure("Device 11 is already in use")
            partitions = drives["IEC Drive"]["partitions"]
            if len(partitions) != 1 or partitions[0]["id"] != 1:
                raise Failure("This suite requires one Software IEC partition, numbered 1")
            original_path = partitions[0]["path"]
            detail(f"{api.rest.json('/v1/info').get('firmware_version')}, seed {args.seed}, {duration:.0f} s")
        saved = Snapshot(args.host, {"SoftIEC Drive Settings": api.configs.category("SoftIEC Drive Settings")})
        started = created = False
        try:
            with check("start the IEC agent and build the fixture"):
                api.configs.set("SoftIEC Drive Settings", "Soft Drive Bus ID", 11)
                api.configs.set("SoftIEC Drive Settings", "IEC Drive", "Enabled")
                session.agent.start()
                started = True
                session.agent.call(OPEN, channel=15)
                session.status()
                session.command("CD//")
                session.root = next(e["IEC Drive"]["partitions"][0]["path"]
                                    for e in api.rest.json("/v1/drives")["drives"] if "IEC Drive" in e)
                session.fixture()
                created = True
            section(f"soak ({duration:.0f} s)")
            with check("the drive keeps answering, keeps its data and keeps its heap"):
                heap, failures = session.run(duration)
                detail(f"{session.iteration} iterations: {dict(sorted(session.counts.items()))}; "
                       f"lanes {session.lane_counts}")
                if session.lane_errors:
                    detail("lane errors: " + "; ".join(session.lane_errors[:10]))
                if len(heap) >= 3:
                    slope = (heap[0] - heap[-1]) / (len(heap) - 1)
                    detail(f"heap free {heap[0]} after warm-up, {heap[-1]} at the end, "
                           f"{slope:.0f} bytes lost per iteration")
                    if slope > LEAK_TOLERANCE_BYTES_PER_ITERATION:
                        failures.append(f"the heap shrank by {slope:.0f} bytes per iteration")
                if failures:
                    detail("recent operations: " + " | ".join(session.history))
                    raise Failure("; ".join(failures))
        except Dead:
            detail("recent operations: " + " | ".join(session.history))
            raise
        finally:
            def restore_directory():
                if started and session.alive():
                    session.agent.call(CLOSE, channel=15)
                    session.agent.call(OPEN, channel=15)
                    session.command("CD//" + original_path[len(session.root or ""):].upper())
                    session.agent.call(CLOSE, channel=15)

            def remove_fixture():
                if created and session.alive():
                    with ftp.session(args.host, args.password) as client:
                        ftp.remove_tree(client, f"{session.root.rstrip('/')}/{session.folder}")

            ok = True
            for label, action in (("restore the IEC working directory", restore_directory),
                                  ("restore the Software IEC settings", lambda: saved.restore(api)),
                                  ("remove this run's directory", remove_fixture),
                                  ("return the C64 to BASIC", lambda: api.machine.reset(force=True))):
                ok = teardown_step(label, action) and ok
    except Exception as exc:
        traceback.print_exc()
        suite_fail(SUITE, str(exc))
        return 1
    suite_ok(SUITE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
