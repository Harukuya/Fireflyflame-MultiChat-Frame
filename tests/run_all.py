"""Run the independent framework acceptance suite. Never access a live host."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def decode(raw):
    result = []
    for line in raw.splitlines(keepends=True):
        try:
            result.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            result.append(line.decode("gbk", errors="replace"))
    return "".join(result)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    node = shutil.which("node")
    if node is None:
        print("Node is needed only for the dashboard tests. Install Node 24 to run this full suite.")
        return 2
    print("Python:", sys.version.split()[0], "Node:", subprocess.check_output([node, "--version"]).decode().strip())
    jobs = [(name, [sys.executable, "-I", "-B", str(TESTS / name), str(ROOT)]) for name in
            ("profile_test.py", "data_test.py", "model_port_test.py", "core_test.py", "runtime_test.py", "dashboard_api_test.py", "local_dashboard_test.py", "universe_host_test.py", "universe_test.py",
             "no_reply233_test.py", "no_reply_state233_test.py", "timeclean234_test.py", "privatecontext234_test.py",
             "window235_test.py", "farewell235_test.py", "activity236_test.py", "activityflow236_test.py",
             "offline_model236_test.py", "look237_test.py", "mission238_test.py",
             "quote239_test.py", "quote_port239_test.py", "quote_excerpt239_test.py", "model_directory240_test.py", "identity240_test.py", "route_scope240_test.py")]
    jobs += [(name, [node, str(TESTS / name), str(ROOT)]) for name in ("frontend_test.cjs", "bridge_test.cjs", "no_reply_dashboard_test.cjs", "local_bridge_test.cjs", "model_routes240_dashboard_test.cjs", "universe_frontend_test.cjs")]
    total, started = 0, time.monotonic()
    for name, command in jobs:
        print(f"\nRUN {name}", flush=True)
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        output = decode(result.stdout)
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        if result.returncode:
            print(f"FAILED: {name}, exit={result.returncode}")
            return result.returncode
        counts = re.findall(r"(?:^|\s)(\d+)/(\d+)(?:\s|$)", output)
        if counts:
            passed, count = map(int, counts[-1])
            if passed != count:
                return 1
        else:
            unit = re.search(r"Ran (\d+) tests?", output)
            if unit is None:
                print("Missing test count:", name)
                return 1
            count = int(unit.group(1))
        total += count
    print(f"\nFRAMEWORK ACCEPTANCE: {total}/{total}; {len(jobs)} scripts; {time.monotonic() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
