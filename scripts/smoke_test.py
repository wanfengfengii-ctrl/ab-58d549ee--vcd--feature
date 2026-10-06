#!/usr/bin/env python3
"""End-to-end HTTP smoke test for a running VCD window service.

It exercises:

* readiness via GET /healthz
* same-timestamp value changes arbitrated in text order
* a cross-timescale window (1ns vs 1ps must give identical fs timelines)
* a time-regression rejection with a locatable error (no partial result)

Exits 0 only when every check passes.
"""

from __future__ import annotations

import io
import json
import os
import sys

import requests

BASE_URL = os.environ.get("VCD_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = 10

HEADER = """$timescale {ts} $end
$scope module top $end
$var wire 1 ! clk $end
$var wire 1 " rst $end
$upscope $end
$enddefinitions $end
"""


def post(body: str, signals, start: int, end: int) -> requests.Response:
    files = {"file": ("wave.vcd", io.BytesIO(body.encode("ascii")), "text/plain")}
    data = {
        "window": json.dumps({"start": start, "end": end}),
        "signals": signals,
    }
    return requests.post(
        f"{BASE_URL}/api/vcd/window",
        files=files,
        data=data,
        timeout=TIMEOUT,
    )


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    # 1. readiness
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=TIMEOUT)
        check("healthz returns 200 ok",
              r.status_code == 200 and r.json().get("status") == "ok",
              f"status={r.status_code} body={r.text[:120]}")
    except requests.RequestException as exc:
        check("healthz returns 200 ok", False, str(exc))
        return 1

    # 2. same-timestamp changes: text-order arbitration + interval merge
    same_time_ns = HEADER.format(ts="1ns") + (
        "#0\n0!\n"
        "#10\n0!\n1!\nx!\n1!\n"
        "#20\n0!\n"
    )
    expected = [
        {"start": 0, "end": 10_000_000, "value": "0"},
        {"start": 10_000_000, "end": 20_000_000, "value": "1"},
    ]
    r = post(same_time_ns, ["top.clk"], 0, 20_000_000)
    timeline = r.json()["signals"]["top.clk"] if r.ok else None
    check("same-instant changes arbitrated in text order",
          r.status_code == 200 and timeline == expected,
          f"status={r.status_code} got={timeline}")

    # 3. cross-timescale window: 1ps trace of the same shape must match
    same_time_ps = HEADER.format(ts="1ps") + (
        "#0\n0!\n"
        "#10000\n0!\n1!\nx!\n1!\n"
        "#20000\n0!\n"
    )
    r2 = post(same_time_ps, ["top.clk"], 0, 20_000_000)
    timeline2 = r2.json()["signals"]["top.clk"] if r2.ok else None
    check("cross-timescale windows agree in femtoseconds",
          r2.status_code == 200 and timeline2 == timeline == expected,
          f"ns={timeline} ps={timeline2}")

    # 4. x/z coverage and multiple signals
    four_state = HEADER.format(ts="1ps") + (
        "#0\nx!\n0\"\n#1000\n0!\nz\"\n#2000\nz!\n1\"\n#3000\n1!\nx\"\n#4000\n0!\n0\"\n"
    )
    r3 = post(four_state, ["top.clk", "top.rst"], 0, 4_000_000)
    ok3 = (
        r3.status_code == 200
        and [s["value"] for s in r3.json()["signals"]["top.clk"]] == ["x", "0", "z", "1"]
        and [s["value"] for s in r3.json()["signals"]["top.rst"]] == ["0", "z", "1", "x"]
        and r3.json()["signals"]["top.clk"][0]["start"] == 0
        and r3.json()["signals"]["top.clk"][-1]["end"] == 4_000_000
    )
    check("0/1/x/z half-open intervals cover the whole window", ok3,
          f"status={r3.status_code} body={r3.text[:200]}")

    # 5. time regression rejected, locatable, no partial result
    regressing = HEADER.format(ts="1ns") + "#10\n0!\n#5\n1!\n"
    r4 = post(regressing, ["top.clk"], 0, 10_000_000)
    err = r4.json().get("error", {}) if r4.content else {}
    check("time regression rejected with a locatable error",
          r4.status_code == 422 and err.get("code") == "TIME_REGRESSION"
          and isinstance(err.get("line"), int) and "signals" not in r4.json(),
          f"status={r4.status_code} body={r4.text[:200]}")

    # 6. window outside the trace is rejected (all-or-nothing)
    r5 = post(same_time_ns, ["top.clk"], 0, 999_000_000)
    err5 = r5.json().get("error", {}) if r5.content else {}
    check("uncovered window rejected without partial results",
          r5.status_code == 422 and err5.get("code") == "WINDOW_NOT_COVERED"
          and "signals" not in r5.json(),
          f"status={r5.status_code} body={r5.text[:200]}")

    if failures:
        print(f"\n{len(failures)} smoke check(s) failed: {failures}")
        return 1
    print("\nall smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
