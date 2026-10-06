"""Tests for VCD parsing and window extraction (app.vcd)."""

from __future__ import annotations

import json

import pytest

from app.vcd import (
    MAX_VALUE_CHANGES,
    VCDError,
    Window,
    parse,
    parse_signals,
    parse_window,
    process,
)
from tests.conftest import build_vcd


# --- happy path -----------------------------------------------------------


def test_basic_window():
    vcd_text = build_vcd(
        """
#0
0! 1"
#5
1!
#10
0!
#20
1!
"""
    )
    result = process(vcd_text, ["top.clk"], Window(0, 20_000_000))
    sig = result["signals"]["top.clk"]
    assert sig == [
        {"start": 0, "end": 5_000_000, "value": "0"},
        {"start": 5_000_000, "end": 10_000_000, "value": "1"},
        {"start": 10_000_000, "end": 20_000_000, "value": "0"},
    ]
    # Last interval covers through the (excluded) window end.
    assert sig[-1]["end"] == 20_000_000


def test_adjacent_same_value_intervals_are_merged():
    vcd_text = build_vcd(
        """
#0
0!
#5
1!
#5
0!
#10
0!
#15
1!
#20
1!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(0, 20_000_000))["signals"]["top.clk"]
    # Final text-order value at t=5 is 0 and t=10 is also 0: the leading
    # zero must extend straight through, with no zero-length gap and no
    # adjacent equal intervals.
    assert all(a["end"] == b["start"] for a, b in zip(sig, sig[1:]))
    assert all(a["value"] != b["value"] for a, b in zip(sig, sig[1:]))
    assert [s["value"] for s in sig] == ["0", "1"]
    assert sig[0]["start"] == 0 and sig[0]["end"] == 15_000_000


def test_same_timestamp_changes_are_text_order_arbitrated():
    vcd_text = build_vcd(
        """
#0
0!
#10
0!
1!
x!
1!
#20
0!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(0, 20_000_000))["signals"]["top.clk"]
    # Final text-order assignment at t=10ns is 1; zero-length intermediate
    # intervals must not appear.
    assert sig == [
        {"start": 0, "end": 10_000_000, "value": "0"},
        {"start": 10_000_000, "end": 20_000_000, "value": "1"},
    ]


def test_window_starts_between_events_uses_effective_value():
    vcd_text = build_vcd(
        """
#0
1!
#10
0!
#20
1!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(5_000_000, 15_000_000))["signals"]["top.clk"]
    assert sig == [
        {"start": 5_000_000, "end": 10_000_000, "value": "1"},
        {"start": 10_000_000, "end": 15_000_000, "value": "0"},
    ]


def test_all_four_levels_x_and_z():
    vcd_text = build_vcd(
        """
#0
x!
#1
0!
#2
z!
#3
1!
#4
X!
#5
Z!
#6
0!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(0, 6_000_000))["signals"]["top.clk"]
    assert [s["value"] for s in sig] == ["x", "0", "z", "1", "x", "z"]
    assert sig[0]["start"] == 0 and sig[-1]["end"] == 6_000_000


def test_window_within_one_interval():
    vcd_text = build_vcd(
        """
#0
1!
#100
0!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(10_000_000, 40_000_000))["signals"]["top.clk"]
    assert sig == [{"start": 10_000_000, "end": 40_000_000, "value": "1"}]


def test_dumpvars_initial_values():
    vcd_text = build_vcd(
        """
#0
$dumpvars
x!
x"
$end
#5
1!
#10
0!
"""
    )
    sig = process(vcd_text, ["top.clk"], Window(0, 10_000_000))["signals"]["top.clk"]
    assert [s["value"] for s in sig] == ["x", "1"]


def test_timescale_conversions():
    for ts, factor in [("1fs", 1), ("10fs", 10), ("100fs", 100),
                       ("1ps", 1_000), ("10ps", 10_000), ("100ps", 100_000),
                       ("1ns", 1_000_000), ("10ns", 10_000_000), ("100ns", 100_000_000)]:
        vcd_text = build_vcd("#0\n0!\n#5\n1!\n#10\n0!\n", timescale=ts)
        sig = process(vcd_text, ["top.clk"], Window(0, 10 * factor))["signals"]["top.clk"]
        assert sig[1] == {"start": 5 * factor, "end": 10 * factor, "value": "1"}, ts


def test_cross_timescale_windows_match_in_femtoseconds():
    ns = build_vcd("#0\n0!\n#10\n1!\n", timescale="1ns")
    ps = build_vcd("#0\n0!\n#10000\n1!\n", timescale="1ps")
    window = Window(0, 10_000_000)
    a = process(ns, ["top.clk"], window)["signals"]["top.clk"]
    b = process(ps, ["top.clk"], window)["signals"]["top.clk"]
    assert a == b


def test_uninitialized_signal_is_x():
    vcd_text = build_vcd(
        "#0\n0!\n#10\n0!\n"
    )
    sig = process(vcd_text, ["top.data"], Window(0, 10_000_000))["signals"]["top.data"]
    assert sig == [{"start": 0, "end": 10_000_000, "value": "x"}]


def test_stable_regardless_of_whitespace_and_declaration_order():
    compact = build_vcd("#0 0! 1\"\n#5 1! 0\"\n#10 0! 1\"\n")
    airy = build_vcd(
        "\n\n\t#0\n\t0!\n\t1\"\n#5\n1!\n0\"\n\t#10\n0!\n1\"\n"
    )
    reordered = build_vcd(
        "#0\n0!\n1\"\n#5\n1!\n0\"\n#10\n0!\n1\"\n",
        signals={'"': "data", "!": "clk"},
    )
    window = Window(0, 10_000_000)
    r1 = process(compact, ["top.clk", "top.data"], window)
    r2 = process(airy, ["top.clk", "top.data"], window)
    r3 = process(reordered, ["top.data", "top.clk"], window)
    assert r1 == r2 == r3


def test_nested_scopes_full_names():
    vcd_text = """$timescale 1ns $end
$scope module top $end
$scope module u_cpu $end
$scope module u_alu $end
$var wire 1 ! q $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
1!
#10
0!
"""
    result = process(vcd_text, ["top.u_cpu.u_alu.q"], Window(0, 10_000_000))
    assert result["signals"]["top.u_cpu.u_alu.q"][0]["value"] == "1"


def test_fused_scalar_change():
    vcd_text = build_vcd(
        "#0\n0!\n#1\n1!\n#2\nx!\n"
    )
    # ! fused with value directly (common VCD style)
    sig = process(vcd_text, ["top.clk"], Window(0, 2_000_000))["signals"]["top.clk"]
    assert [s["value"] for s in sig] == ["0", "1"]


def test_vector_style_change_on_one_bit_wire():
    vcd_text = build_vcd("#0\nb0 !\n#1\nb1 !\n#2\nbx !\n#3\nb0 !\n")
    sig = process(vcd_text, ["top.clk"], Window(0, 3_000_000))["signals"]["top.clk"]
    assert [s["value"] for s in sig] == ["0", "1", "x"]


def test_change_limit_enforced():
    body = "#0\n" + " ".join(f"0!" for _ in range(100)) + "\n#1\n"
    body += "\n".join(f"#{i}\n1!\n0!\n" for i in range(2, 30000))
    vcd_text = build_vcd(body)
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "TOO_MANY_CHANGES"


def test_exactly_at_change_limit_ok():
    n = MAX_VALUE_CHANGES
    # one change per timestamp keeps it under 50k timestamps
    body = "\n".join(f"#{i}\n{'1' if i % 2 else '0'}!\n" for i in range(n))
    vcd_text = build_vcd(body)
    vcd = parse(vcd_text)
    assert len(vcd.changes["!"]) == n


# --- error cases ----------------------------------------------------------


def test_time_regression_rejected_with_location():
    vcd_text = build_vcd("#0\n0!\n#10\n1!\n#5\n0!\n")
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "TIME_REGRESSION"
    assert exc.value.line is not None


def test_equal_timestamps_are_allowed():
    vcd_text = build_vcd("#10\n1!\n#10\n0!\n#20\n1!\n")
    vcd = parse(vcd_text)
    assert [t for t, _ in vcd.changes["!"]] == [10_000_000, 10_000_000, 20_000_000]


def test_bad_timescales_rejected():
    for ts in ["1us", "2ns", "1ms", "0ns", "1000fs", "1.0ns", "10 s"]:
        with pytest.raises(VCDError) as exc:
            parse(build_vcd("#0\n0!\n", timescale=ts.strip()))
        assert exc.value.code == "UNSUPPORTED_TIMESCALE", ts


def test_non_wire_rejected():
    vcd_text = """$timescale 1ns $end
$scope module top $end
$var reg 1 ! q $end
$upscope $end
$enddefinitions $end
#0
0!
"""
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "UNSUPPORTED_VAR"


def test_multi_bit_wire_rejected():
    vcd_text = """$timescale 1ns $end
$scope module top $end
$var wire 8 ! bus $end
$upscope $end
$enddefinitions $end
#0
b00000000 !
"""
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "UNSUPPORTED_VAR"


def test_undeclared_identifier_change_rejected():
    vcd_text = build_vcd("#0\n0!\n#5\n1#\n")
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "UNDECLARED_IDENTIFIER"
    assert exc.value.line is not None


def test_undeclared_selected_signal_rejected():
    vcd_text = build_vcd("#0\n0!\n#10\n0!\n")
    with pytest.raises(VCDError) as exc:
        process(vcd_text, ["top.missing"], Window(0, 10_000_000))
    assert exc.value.code == "UNDECLARED_SIGNAL"


def test_duplicate_signal_name_in_declaration_rejected():
    vcd_text = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! a $end
$var wire 1 " a $end
$upscope $end
$enddefinitions $end
#0
0!
"""
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "DUPLICATE_NAME"


def test_duplicate_scope_rejected():
    vcd_text = """$timescale 1ns $end
$scope module top $end
$scope module u $end
$upscope $end
$scope module u $end
$upscope $end
$upscope $end
$enddefinitions $end
"""
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "DUPLICATE_NAME"


def test_duplicate_selection_rejected():
    with pytest.raises(VCDError) as exc:
        parse_signals(["top.a", "top.a"])
    assert exc.value.code == "DUPLICATE_SIGNAL"
    with pytest.raises(VCDError):
        parse_signals(json.dumps(["top.a", "top.a"]))


def test_window_start_before_trace_rejected():
    vcd_text = build_vcd("#5\n0!\n#10\n1!\n")
    with pytest.raises(VCDError) as exc:
        process(vcd_text, ["top.clk"], Window(0, 10_000_000))
    assert exc.value.code == "WINDOW_NOT_COVERED"


def test_window_end_after_trace_rejected():
    vcd_text = build_vcd("#0\n0!\n#10\n1!\n")
    with pytest.raises(VCDError) as exc:
        process(vcd_text, ["top.clk"], Window(0, 20_000_000))
    assert exc.value.code == "WINDOW_NOT_COVERED"


def test_window_exactly_bounds_ok():
    vcd_text = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n")
    sig = process(vcd_text, ["top.clk"], Window(0, 20_000_000))["signals"]["top.clk"]
    assert sig[-1]["end"] == 20_000_000


def test_syntax_error_reports_line():
    vcd_text = build_vcd("#0\n0!\n#10\nwhatisthis\n")
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "VCD_SYNTAX_ERROR"
    assert exc.value.line is not None


def test_real_change_rejected():
    vcd_text = build_vcd("#0\nr3.14 !\n")
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "UNSUPPORTED_VAR"


def test_change_before_timestamp_rejected():
    vcd_text = build_vcd("0!\n#0\n0!\n")
    with pytest.raises(VCDError) as exc:
        parse(vcd_text)
    assert exc.value.code == "VCD_SYNTAX_ERROR"


def test_window_validation():
    with pytest.raises(VCDError) as exc:
        parse_window(json.dumps({"start": 10, "end": 5}))
    assert exc.value.code == "INVALID_WINDOW"
    with pytest.raises(VCDError):
        parse_window(json.dumps({"start": 0, "end": 0}))
    with pytest.raises(VCDError):
        parse_window("not json")
    with pytest.raises(VCDError):
        parse_window(None)
    w = parse_window(json.dumps({"start": 0, "end": 100}))
    assert w.start == 0 and w.end == 100


def test_no_partial_result_on_late_error():
    # A bad change at the end must fail the whole parse; nothing is returned.
    vcd_text = build_vcd("#0\n0!\n#5\n1!\n#10\n2!\n")
    with pytest.raises(VCDError):
        process(vcd_text, ["top.clk"], Window(0, 10_000_000))
