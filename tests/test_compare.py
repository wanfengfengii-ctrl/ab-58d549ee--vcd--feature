"""Tests for golden/candidate VCD comparison (app.vcd.compare)."""

from __future__ import annotations

import json

import pytest

from app.vcd import (
    MAX_PAIRS,
    VCDError,
    Pair,
    Window,
    compare,
    parse_pairs,
)
from tests.conftest import build_vcd


NS = build_vcd(
    "#0\n0!\n#5\n1!\n#10\n0!\n#15\n1!\n#20\n0!\n",
    timescale="1ns",
)
# Same logical waveform expressed in picoseconds.
PS = build_vcd(
    "#0\n0!\n#5000\n1!\n#10000\n0!\n#15000\n1!\n#20000\n0!\n",
    timescale="1ps",
)
WINDOW = Window(0, 20_000_000)
PAIR = [Pair(index=0, reference="top.clk", candidate="top.clk")]


def pair_result(result: dict, index: int = 0) -> dict:
    return result["pairs"][index]


# --- happy paths ----------------------------------------------------------


def test_identical_waveforms_have_no_mismatch():
    result = compare(NS, NS, PAIR, WINDOW)
    pr = pair_result(result)
    assert pr["mismatches"] == []
    assert pr["totalMismatchFs"] == 0


def test_cross_timescale_logically_equivalent_is_consistent():
    result = compare(NS, PS, PAIR, WINDOW)
    pr = pair_result(result)
    assert pr["mismatches"] == []
    assert pr["totalMismatchFs"] == 0
    assert result["window"] == {"start": 0, "end": 20_000_000, "unit": "fs"}


def test_real_level_divergence_reports_half_open_interval():
    # Candidate stays 0 for one extra ns between t=10ns and t=11ns.
    cand = build_vcd(
        "#0\n0!\n#5\n1!\n#11\n0!\n#15\n1!\n#20\n0!\n",
        timescale="1ns",
    )
    result = compare(NS, cand, PAIR, WINDOW)
    pr = pair_result(result)
    assert pr["mismatches"] == [
        {
            "start": 10_000_000,
            "end": 11_000_000,
            "reference": "0",
            "candidate": "1",
            "mismatchFs": 1_000_000,
        }
    ]
    assert pr["totalMismatchFs"] == 1_000_000


def test_mismatch_only_inside_window():
    # Divergence exists before the window start; inside the window both
    # waveforms are identical, so nothing is reported.
    ref = build_vcd("#0\n0!\n#5\n1!\n#10\n0!\n", timescale="1ns")
    cand = build_vcd("#0\n1!\n#5\n1!\n#10\n0!\n", timescale="1ns")
    result = compare(ref, cand, PAIR, Window(5_000_000, 10_000_000))
    pr = pair_result(result)
    assert pr["mismatches"] == []
    assert pr["totalMismatchFs"] == 0


def test_divergence_running_to_window_end_is_half_open():
    ref = build_vcd("#0\n0!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    cand = build_vcd("#0\n0!\n#10\n1!\n#20\n1!\n", timescale="1ns")
    result = compare(ref, cand, PAIR, WINDOW)
    pr = pair_result(result)
    assert pr["mismatches"] == [
        {
            "start": 10_000_000,
            "end": 20_000_000,
            "reference": "0",
            "candidate": "1",
            "mismatchFs": 10_000_000,
        }
    ]


def test_instant_level_disagreement_has_zero_duration():
    # Both move 0 -> 1 at exactly t=10ns: at no femtosecond do levels differ.
    ref = build_vcd("#0\n0!\n#10\n1!\n#20\n1!\n", timescale="1ns")
    cand = build_vcd(
        "#0\n0!\n#5\n1!\n#10\n1!\n#20\n1!\n",
        timescale="1ns",
    )
    # ... here candidate is 1 from 5ns too, so expect [5ns,10ns) mismatch;
    # the change at 10ns is simultaneous and must add no interval.
    pr = pair_result(compare(ref, cand, PAIR, WINDOW))
    assert pr["mismatches"] == [
        {
            "start": 5_000_000,
            "end": 10_000_000,
            "reference": "0",
            "candidate": "1",
            "mismatchFs": 5_000_000,
        }
    ]

    # Truly simultaneous opposite edges: kinds differ, but both are real.
    ref2 = build_vcd("#0\n0!\n#10\n1!\n#20\n1!\n", timescale="1ns")
    cand2 = build_vcd("#0\n1!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    pr2 = pair_result(compare(ref2, cand2, PAIR, WINDOW))
    assert pr2["mismatches"] == [
        {
            "start": 0,
            "end": 10_000_000,
            "reference": "0",
            "candidate": "1",
            "mismatchFs": 10_000_000,
        },
        {
            "start": 10_000_000,
            "end": 20_000_000,
            "reference": "1",
            "candidate": "0",
            "mismatchFs": 10_000_000,
        },
    ]


def test_adjacent_same_kind_differences_are_merged():
    # Reference toggles 0->1->0 while candidate is stuck at 1. The agreeing
    # middle slice separates two (0,1) runs: they stay apart; meanwhile a
    # candidate-only redundant edge must not split a run.
    ref = build_vcd("#0\n0!\n#5\n1!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    cand = build_vcd(
        "#0\n1!\n#3\n1!\n#10\n1!\n#20\n1!\n",  # redundant 1 assignments
        timescale="1ns",
    )
    pr = pair_result(compare(ref, cand, PAIR, WINDOW))
    assert pr["mismatches"] == [
        {"start": 0, "end": 5_000_000, "reference": "0", "candidate": "1",
         "mismatchFs": 5_000_000},
        {"start": 10_000_000, "end": 20_000_000, "reference": "0", "candidate": "1",
         "mismatchFs": 10_000_000},
    ]
    assert pr["totalMismatchFs"] == 15_000_000


def test_pairs_returned_in_request_order_with_indices():
    pairs = [
        Pair(index=7, reference="top.data", candidate="top.data"),
        Pair(index=2, reference="top.clk", candidate="top.clk"),
    ]
    result = compare(NS, NS, pairs, WINDOW)
    assert [p["index"] for p in result["pairs"]] == [7, 2]
    assert result["pairs"][0]["reference"] == "top.data"
    assert result["pairs"][0]["candidate"] == "top.data"


def test_pairing_can_map_different_hierarchical_names():
    ref = build_vcd("#0\n0!\n#10\n1!\n#20\n1!\n", timescale="1ns")
    cand = build_vcd("#0\n0\"\n#10\n0\"\n#20\n0\"\n", timescale="1ns")
    pairs = [Pair(index=3, reference="top.clk", candidate="top.data")]
    pr = pair_result(compare(ref, cand, pairs, WINDOW))
    assert pr["index"] == 3
    assert pr["mismatches"] == [
        {"start": 10_000_000, "end": 20_000_000, "reference": "1",
         "candidate": "0", "mismatchFs": 10_000_000},
    ]


def test_x_z_levels_participate_in_comparison():
    ref = build_vcd("#0\nx!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    cand = build_vcd("#0\nz!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    pr = pair_result(compare(ref, cand, PAIR, WINDOW))
    assert pr["mismatches"] == [
        {"start": 0, "end": 10_000_000, "reference": "x", "candidate": "z",
         "mismatchFs": 10_000_000},
    ]


# --- request-shape errors -------------------------------------------------


def test_parse_pairs_json_array():
    raw = json.dumps(
        [{"index": 1, "reference": "top.a", "candidate": "top.b"}]
    )
    pairs = parse_pairs([raw])
    assert pairs == [Pair(1, "top.a", "top.b")]


def test_parse_pairs_repeated_fields():
    pairs = parse_pairs([
        json.dumps({"index": 0, "reference": "a", "candidate": "b"}),
        json.dumps({"index": 1, "reference": "c", "candidate": "d"}),
    ])
    assert [(p.index, p.reference, p.candidate) for p in pairs] == [
        (0, "a", "b"), (1, "c", "d"),
    ]


def test_parse_pairs_single_object_accepted():
    pairs = parse_pairs(
        [json.dumps({"index": 0, "reference": "a", "candidate": "b"})]
    )
    assert len(pairs) == 1


def test_parse_pairs_requires_at_least_one():
    with pytest.raises(VCDError) as exc:
        parse_pairs([])
    assert exc.value.code == "MISSING_FIELD"
    assert exc.value.side == "comparison"
    with pytest.raises(VCDError) as exc:
        parse_pairs([json.dumps([])])
    assert exc.value.code == "INVALID_PAIRS"


def test_parse_pairs_limit():
    raw = json.dumps(
        [{"index": i, "reference": "a", "candidate": "b"}
         for i in range(MAX_PAIRS + 1)]
    )
    with pytest.raises(VCDError) as exc:
        parse_pairs([raw])
    assert exc.value.code == "INVALID_PAIRS"
    assert exc.value.details["max"] == MAX_PAIRS


def test_parse_pairs_boundary_count_ok():
    raw = json.dumps(
        [{"index": i, "reference": "a", "candidate": "b"}
         for i in range(MAX_PAIRS)]
    )
    assert len(parse_pairs([raw])) == MAX_PAIRS


def test_parse_pairs_duplicate_index_rejected():
    raw = json.dumps([
        {"index": 5, "reference": "a", "candidate": "b"},
        {"index": 5, "reference": "c", "candidate": "d"},
    ])
    with pytest.raises(VCDError) as exc:
        parse_pairs([raw])
    assert exc.value.code == "DUPLICATE_PAIR"
    assert exc.value.side == "comparison"
    assert exc.value.details == {"index": 5}


def test_parse_pairs_bad_shape_rejected():
    bad_payloads = [
        json.dumps([{"index": "1", "reference": "a", "candidate": "b"}]),
        json.dumps([{"index": -1, "reference": "a", "candidate": "b"}]),
        json.dumps([{"index": 0, "reference": "", "candidate": "b"}]),
        json.dumps([{"index": 0, "reference": "a"}]),
        json.dumps([{"index": 0, "reference": 3, "candidate": "b"}]),
        "not-json",
    ]
    for payload in bad_payloads:
        with pytest.raises(VCDError) as exc:
            parse_pairs([payload])
        assert exc.value.code in ("INVALID_PAIRS",), payload


# --- file-level errors, side-tagged and all-or-nothing --------------------


def test_reference_parse_error_tagged():
    bad = build_vcd("#0\n0!\n#10\n!!!bogus\n")
    with pytest.raises(VCDError) as exc:
        compare(bad, NS, PAIR, WINDOW)
    assert exc.value.side == "reference"
    assert exc.value.line is not None


def test_candidate_parse_error_tagged():
    bad = build_vcd("#10\n0!\n#5\n1!\n")
    with pytest.raises(VCDError) as exc:
        compare(NS, bad, PAIR, WINDOW)
    assert exc.value.side == "candidate"
    assert exc.value.code == "TIME_REGRESSION"


def test_missing_reference_signal_tagged():
    with pytest.raises(VCDError) as exc:
        compare(NS, NS, [Pair(0, "top.nope", "top.clk")], WINDOW)
    assert exc.value.side == "reference"
    assert exc.value.code == "UNDECLARED_SIGNAL"
    assert "top.nope" in exc.value.details["signals"]


def test_missing_candidate_signal_tagged():
    with pytest.raises(VCDError) as exc:
        compare(NS, NS, [Pair(0, "top.clk", "top.nope")], WINDOW)
    assert exc.value.side == "candidate"
    assert exc.value.code == "UNDECLARED_SIGNAL"


def test_window_not_covered_tagged_per_side():
    short = build_vcd("#0\n0!\n#10\n1!\n", timescale="1ns")
    with pytest.raises(VCDError) as exc:
        compare(short, NS, PAIR, WINDOW)
    assert exc.value.side == "reference"
    assert exc.value.code == "WINDOW_NOT_COVERED"

    with pytest.raises(VCDError) as exc:
        compare(NS, short, PAIR, WINDOW)
    assert exc.value.side == "candidate"
    assert exc.value.code == "WINDOW_NOT_COVERED"


def test_failure_produces_no_partial_results():
    # Even though the first pair would be fine, a missing signal in a later
    # pair fails the whole request.
    pairs = [
        Pair(0, "top.clk", "top.clk"),
        Pair(1, "top.data", "top.ghost"),
    ]
    with pytest.raises(VCDError):
        compare(NS, NS, pairs, WINDOW)


def test_change_limit_applies_per_file():
    from app.vcd import MAX_VALUE_CHANGES

    body = "\n".join(
        f"#{i}\n{'1' if i % 2 else '0'}!\n"
        for i in range(MAX_VALUE_CHANGES + 1)
    )
    huge = build_vcd(body)
    with pytest.raises(VCDError) as exc:
        compare(huge, NS, PAIR, WINDOW)
    assert exc.value.code == "TOO_MANY_CHANGES"
    assert exc.value.side == "reference"


def test_change_limit_is_combined_across_both_files():
    from app.vcd import MAX_VALUE_CHANGES

    half = MAX_VALUE_CHANGES // 2
    body = "\n".join(
        f"#{i}\n{'1' if i % 2 else '0'}!\n" for i in range(half + 1)
    )
    big = build_vcd(body)
    # Each file alone is under 50k; together they exceed it, so the
    # candidate side trips the shared per-request budget.
    with pytest.raises(VCDError) as exc:
        compare(big, big, PAIR, WINDOW)
    assert exc.value.code == "TOO_MANY_CHANGES"
    assert exc.value.side == "candidate"
    assert exc.value.details["max_changes_per_request"] == MAX_VALUE_CHANGES


def test_combined_change_limit_boundary_ok():
    from app.vcd import MAX_VALUE_CHANGES

    # 40k + 10k changes across two files sits exactly on the budget.
    def make(n_changes: int) -> str:
        return build_vcd(
            "\n".join(f"#{i}\n{'1' if i % 2 else '0'}!\n"
                      for i in range(n_changes))
        )

    ref = make(40_000)
    cand = make(10_000)
    # Both traces cover through 9999 ns; use a window inside that range.
    result = compare(ref, cand, PAIR, Window(0, 9_999_000_000))
    assert result["pairs"][0]["totalMismatchFs"] >= 0
