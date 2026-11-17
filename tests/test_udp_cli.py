"""can_logger and can_replayer on localhost buses (--udp-multicast), end to end, no hardware.

A synthetic candump log with two buses is replayed by can_replayer onto two localhost buses and
recorded by can_logger to a TRC file; every frame must arrive, on the right bus, in order. Each run
uses its own groups and ports (239.99.0.0/24, 47800-47999), so it cannot hear another test's buses.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from canbench.parsers import peak_trc  # noqa: E402
from canbench.parsers.base import CANFrame  # noqa: E402

GROUPS = ["239.99.0.221:47931", "239.99.0.222:47932"]


def _candump(path, n=50):
    t = 1794586450.0
    with open(path, "w") as fh:
        for i in range(n):
            fh.write(f"({t + i * 0.01:.6f}) can0 101#{i:02X}\n")
            fh.write(f"({t + i * 0.01 + 0.002:.6f}) can1 202#{i:02X}AA\n")


def _run(cmd, out):
    return subprocess.Popen([sys.executable, "-B"] + cmd, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def test_replay_onto_localhost_buses_into_trc():
    d = Path(tempfile.mkdtemp(prefix="can-tools-udp-cli-"))
    src, trc = d / "src.log", d / "cap.trc"
    _candump(src)
    udp = [x for g in GROUPS for x in ("--udp-multicast", g)]
    with open(d / "logger.txt", "w") as lo:
        lg = _run([str(ROOT / "tools/can_logger.py"), "-o", str(trc)] + udp, lo)
        for _ in range(150):
            if "Starting logging" in (d / "logger.txt").read_text() or lg.poll() is not None:
                break
            time.sleep(0.1)
        assert lg.poll() is None, (d / "logger.txt").read_text()[-800:]
        with open(d / "replayer.txt", "w") as ro:
            rp = _run([str(ROOT / "tools/can_replayer.py"), str(src), "--skip-confirmation"] + udp
                      + ["--adapter", f"0=udp:{GROUPS[0].split(':')[0]}",
                         "--adapter", f"1=udp:{GROUPS[1].split(':')[0]}"], ro)
            rc = rp.wait(timeout=60)
        time.sleep(0.5)
        lg.send_signal(signal.SIGINT)
        lg.wait(timeout=30)
    assert rc == 0, (d / "replayer.txt").read_text()[-800:]
    per = {}
    for r in peak_trc.PeakTrcParser().parse_file(trc):
        if isinstance(r, CANFrame):
            per.setdefault(r.bus, []).append((r.arbitration_id, bytes(r.data)))
    assert sorted(per) == [1, 2], per.keys()
    assert per[1] == [(0x101, bytes([i])) for i in range(50)], per[1][:3]
    assert per[2] == [(0x202, bytes([i, 0xAA])) for i in range(50)], per[2][:3]


def test_shared_port_refused_by_both_tools():
    d = Path(tempfile.mkdtemp(prefix="can-tools-udp-cli-"))
    src = d / "src.log"
    _candump(src, n=2)
    same = ["--udp-multicast", "239.99.0.223:47933", "--udp-multicast", "239.99.0.224:47933"]
    for cmd in ([str(ROOT / "tools/can_logger.py"), "-o", str(d / "x.trc")] + same,
                [str(ROOT / "tools/can_replayer.py"), str(src), "--skip-confirmation"] + same):
        p = subprocess.run([sys.executable, "-B"] + cmd, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        assert p.returncode == 1 and "own port" in p.stdout + p.stderr, (p.returncode, (p.stdout + p.stderr)[-400:])


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS  {name}")
            except AssertionError as e:
                failures += 1; print(f"FAIL  {name}: {e}")
            except Exception as e:
                failures += 1; print(f"ERROR {name}: {e!r}")
    print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
    sys.exit(1 if failures else 0)
