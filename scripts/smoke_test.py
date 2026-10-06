#!/usr/bin/env python3
"""End-to-end HTTP smoke test for a running VCD window service.

It exercises:

* readiness via GET /healthz
* same-timestamp value changes arbitrated in text order
* a cross-timescale window (1ns vs 1ps must give identical fs timelines)
* a time-regression rejection with a locatable error (no partial result)
* golden/candidate comparison over POST /api/vcd/compare, including a
  cross-timescale match, real level divergence and side-tagged failures

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


def post_compare(ref_body: str, cand_body: str, pairs, start: int, end: int):
    files = {
        "reference": ("ref.vcd", io.BytesIO(ref_body.encode("ascii")), "text/plain"),
        "candidate": ("cand.vcd", io.BytesIO(cand_body.encode("ascii")), "text/plain"),
    }
    data = {
        "window": json.dumps({"start": start, "end": end}),
        "pairs": json.dumps(pairs),
    }
    resp = requests.post(
        f"{BASE_URL}/api/vcd/compare",
        files=files,
        data=data,
        timeout=TIMEOUT,
    )
    return resp


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

    # 7. compare: cross-timescale logically equivalent waveforms agree
    ref_ns = HEADER.format(ts="1ns") + (
        "#0\n0!\n0\"\n#10\n1!\n1\"\n#20\n0!\n0\"\n"
    )
    cand_ps = HEADER.format(ts="1ps") + (
        "#0\n0!\n0\"\n#10000\n1!\n1\"\n#20000\n0!\n0\"\n"
    )
    pairs = [
        {"index": 0, "reference": "top.clk", "candidate": "top.clk"},
        {"index": 1, "reference": "top.rst", "candidate": "top.rst"},
    ]
    rc = post_compare(ref_ns, cand_ps, pairs, 0, 20_000_000)
    ok_c = (
        rc.status_code == 200
        and [p["index"] for p in rc.json()["pairs"]] == [0, 1]
        and all(p["mismatches"] == [] and p["totalMismatchFs"] == 0
                for p in rc.json()["pairs"])
    )
    check("compare: cross-timescale equivalent waveforms match", ok_c,
          f"status={rc.status_code} body={rc.text[:200]}")

    # 8. compare: real level divergence is bounded to its actual duration;
    #    mismatch intervals are half-open and carry both levels.
    #    golden clk : 0[0,10) 1[10,20) ; golden rst: 0[0,10) 1[10,20)
    #    cand   clk : 0[0,5) 1[5,15) 0[15,20)
    #    cand   rst : 0[0,10) 1[10,15) 0[15,20)
    divergent = HEADER.format(ts="1ns") + (
        "#0\n0!\n0\"\n#5\n1!\n#10\n1\"\n#15\n0!\n0\"\n#20\n0!\n0\"\n"
    )
    rd = post_compare(ref_ns, divergent, pairs, 0, 20_000_000)
    d_body = rd.json() if rd.content else {}
    d_pairs = d_body.get("pairs") or []
    d_clk = d_pairs[0]["mismatches"] if len(d_pairs) > 0 else None
    d_rst = d_pairs[1]["mismatches"] if len(d_pairs) > 1 else None
    expected_clk = [
        {"start": 5_000_000, "end": 10_000_000, "reference": "0",
         "candidate": "1", "mismatchFs": 5_000_000},
        {"start": 15_000_000, "end": 20_000_000, "reference": "1",
         "candidate": "0", "mismatchFs": 5_000_000},
    ]
    expected_rst = [
        {"start": 15_000_000, "end": 20_000_000, "reference": "1",
         "candidate": "0", "mismatchFs": 5_000_000},
    ]
    check("compare: mismatches bounded to actual differing half-open intervals",
          rd.status_code == 200 and d_clk == expected_clk and d_rst == expected_rst
          and d_pairs[0]["totalMismatchFs"] == 10_000_000,
          f"status={rd.status_code} body={rd.text[:240]}")

    # 9. compare: divergent changes exactly at window end count for nothing
    same_to_end = HEADER.format(ts="1ns") + (
        "#0\n0!\n#10\n1!\n#20\n0!\n#30\n1!\n"
    )
    re_ = post_compare(same_to_end, same_to_end,
                       [pairs[0]], 0, 20_000_000)
    check("compare: identical window reports zero duration and no diffs",
          re_.status_code == 200
          and re_.json()["pairs"][0]["mismatches"] == []
          and re_.json()["pairs"][0]["totalMismatchFs"] == 0,
          f"status={re_.status_code} body={re_.text[:200]}")

    # 10. compare: failures are side-tagged and carry no partial results
    bad_cand = HEADER.format(ts="1ns") + "#0\n0!\n#10\n1!\n#5\n0!\n"
    rx = post_compare(ref_ns, bad_cand, pairs, 0, 20_000_000)
    ex = rx.json().get("error", {}) if rx.content else {}
    check("compare: candidate parse error tagged candidate, no partial results",
          rx.status_code == 422 and ex.get("code") == "TIME_REGRESSION"
          and ex.get("side") == "candidate" and "pairs" not in rx.json(),
          f"status={rx.status_code} body={rx.text[:200]}")

    dup_pairs = pairs + [
        {"index": 0, "reference": "top.clk", "candidate": "top.clk"}
    ]
    ry = post_compare(ref_ns, cand_ps, dup_pairs, 0, 20_000_000)
    ey = ry.json().get("error", {}) if ry.content else {}
    check("compare: duplicate pair index rejected as comparison error",
          ry.status_code == 400 and ey.get("code") == "DUPLICATE_PAIR"
          and ey.get("side") == "comparison",
          f"status={ry.status_code} body={ry.text[:200]}")

    missing = [{"index": 0, "reference": "top.clk", "candidate": "top.ghost"}]
    rz = post_compare(ref_ns, cand_ps, missing, 0, 20_000_000)
    ez = rz.json().get("error", {}) if rz.content else {}
    check("compare: missing candidate signal tagged candidate",
          rz.status_code == 400 and ez.get("code") == "UNDECLARED_SIGNAL"
          and ez.get("side") == "candidate",
          f"status={rz.status_code} body={rz.text[:200]}")

    if failures:
        print(f"\n{len(failures)} smoke check(s) failed: {failures}")
        return 1
    print("\nall smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
