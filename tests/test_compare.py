"""Unit tests for golden-vs-candidate comparison (app.vcd.compare)."""

from __future__ import annotations

import json

import pytest

from app.vcd import (
    MAX_PAIRS,
    VCDError,
    Window,
    compare,
    parse_pairs,
)
from tests.conftest import build_vcd


WINDOW = Window(0, 20_000_000)
PAIR = [("top.clk", "top.clk")]


def cmp(reference_text, candidate_text, pairs=PAIR, window=WINDOW):
    from app.vcd import SignalPair

    signal_pairs = [SignalPair(reference=r, candidate=c) for r, c in pairs]
    return compare(reference_text, candidate_text, signal_pairs, window)


# --- happy paths ----------------------------------------------------------


def test_identical_waveforms_have_no_differences():
    vcd = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n")
    result = cmp(vcd, vcd)
    assert result["pairs"][0]["differences"] == []
    assert result["pairs"][0]["mismatchFs"] == 0
    assert result["mismatchFs"] == 0


def test_cross_timescale_logically_equivalent_is_consistent():
    ns = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n", timescale="1ns")
    ps = build_vcd("#0\n0!\n#10000\n1!\n#20000\n0!\n", timescale="1ps")
    result = cmp(ns, ps)
    pair = result["pairs"][0]
    assert pair["differences"] == []
    assert pair["mismatchFs"] == 0
    assert result["mismatchFs"] == 0


def test_real_level_divergence_reports_only_its_interval():
    reference = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n", timescale="1ns")
    candidate = build_vcd(
        "#0\n0!\n#10000\n1!\n#15000\n0!\n#20000\n0!\n", timescale="1ps"
    )
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == [
        {
            "start": 15_000_000,
            "end": 20_000_000,
            "referenceValue": "1",
            "candidateValue": "0",
        }
    ]
    assert result["pairs"][0]["mismatchFs"] == 5_000_000
    assert result["mismatchFs"] == 5_000_000


def test_divergence_is_reported_in_femtoseconds():
    reference = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n", timescale="1ns")
    candidate = build_vcd(
        "#0\n0!\n#10000\n0!\n#20000\n0!\n", timescale="1ps"
    )
    result = cmp(reference, candidate)
    # Candidate stays 0 while reference is 1 on [10ns, 20ns).
    assert result["pairs"][0]["differences"] == [
        {
            "start": 10_000_000,
            "end": 20_000_000,
            "referenceValue": "1",
            "candidateValue": "0",
        }
    ]
    assert result["pairs"][0]["mismatchFs"] == 10_000_000


def test_multiple_disjoint_divergences():
    # reference: 0[0,5) 1[5,15) 0[15,20)
    reference = build_vcd("#0\n0!\n#5\n1!\n#15\n0!\n#20\n0!\n", timescale="1ns")
    # candidate: 1[0,5) 1[5,15) 1[15,20) -> mismatches at the two 0 stretches
    candidate = build_vcd(
        "#0\n1!\n#5000\n1!\n#15000\n1!\n#20000\n1!\n", timescale="1ps"
    )
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == [
        {"start": 0, "end": 5_000_000, "referenceValue": "0", "candidateValue": "1"},
        {"start": 15_000_000, "end": 20_000_000,
         "referenceValue": "0", "candidateValue": "1"},
    ]
    assert result["pairs"][0]["mismatchFs"] == 10_000_000


def test_adjacent_same_kind_differences_are_merged():
    # Reference stays 0 the whole window; a same-timestamp no-op reassignment
    # at 5ns still creates an interior boundary.
    reference = build_vcd("#0\n0!\n#5\n1!\n0!\n#10\n0!\n#20\n0!\n", timescale="1ns")
    # Candidate is 1 across that boundary: (0,1) mismatch on both sides must
    # merge into one interval.
    candidate = build_vcd(
        "#0\n0!\n#3000\n1!\n#5000\n1!\n#7000\n0!\n#20000\n0!\n", timescale="1ps"
    )
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == [
        {"start": 3_000_000, "end": 7_000_000,
         "referenceValue": "0", "candidateValue": "1"}
    ]
    assert result["pairs"][0]["mismatchFs"] == 4_000_000


def test_different_level_pairs_are_not_merged():
    # reference: 0 throughout
    reference = build_vcd("#0\n0!\n#20\n0!\n", timescale="1ns")
    # candidate: 1 on [4ns,8ns), x on [8ns,12ns), 0 otherwise
    candidate = build_vcd(
        "#0\n0!\n#4000\n1!\n#8000\nx!\n#12000\n0!\n#20000\n0!\n",
        timescale="1ps",
    )
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == [
        {"start": 4_000_000, "end": 8_000_000,
         "referenceValue": "0", "candidateValue": "1"},
        {"start": 8_000_000, "end": 12_000_000,
         "referenceValue": "0", "candidateValue": "x"},
    ]
    assert result["pairs"][0]["mismatchFs"] == 8_000_000


def test_equal_gap_separates_same_kind_mismatches():
    # Two (0,1) mismatches with a real equal stretch between stay separate.
    reference = build_vcd("#0\n0!\n#20\n1!\n", timescale="1ns")
    candidate = build_vcd(
        "#0\n1!\n#4000\n0!\n#12000\n1!\n#16000\n0!\n#20000\n1!\n",
        timescale="1ps",
    )
    result = cmp(reference, candidate)["pairs"][0]
    assert result["differences"] == [
        {"start": 0, "end": 4_000_000, "referenceValue": "0", "candidateValue": "1"},
        {"start": 12_000_000, "end": 16_000_000,
         "referenceValue": "0", "candidateValue": "1"},
    ]
    assert result["mismatchFs"] == 8_000_000


def test_window_start_in_the_middle_uses_effective_levels():
    reference = build_vcd("#0\n1!\n#10\n0!\n#20\n1!\n", timescale="1ns")
    candidate = build_vcd("#0\n1!\n#10000\n1!\n#20000\n1!\n", timescale="1ps")
    window = Window(5_000_000, 20_000_000)
    result = cmp(reference, candidate, PAIR, window)
    # reference 0 on [10ns,20ns), candidate 1 -> single mismatch.
    assert result["pairs"][0]["differences"] == [
        {"start": 10_000_000, "end": 20_000_000,
         "referenceValue": "0", "candidateValue": "1"}
    ]
    assert result["window"] == {"start": 5_000_000, "end": 20_000_000, "unit": "fs"}


def test_pairs_are_returned_in_submission_order_with_names():
    vcd = build_vcd(
        "#0\n0!\n0\"\n#20\n1!\n1\"\n",
        signals={"!": "clk", '"': "rst"},
    )
    result = cmp(
        vcd, vcd,
        [("top.rst", "top.clk"), ("top.clk", "top.rst")],
    )
    ordered = [(p["reference"], p["candidate"]) for p in result["pairs"]]
    assert ordered == [("top.rst", "top.clk"), ("top.clk", "top.rst")]


def test_mismatch_totals_aggregate_per_pair():
    reference = build_vcd("#0\n0!\n0\"\n#20\n0!\n0\"\n",
                          signals={"!": "clk", '"': "rst"})
    # clk diverges for 10ns ([0,10ns)); rst diverges for 5ns ([0,5ns)).
    candidate = build_vcd(
        "#0\n1!\n1\"\n#5000\n0\"\n#10000\n0!\n#20000\n0!\n0\"\n",
        timescale="1ps",
        signals={"!": "clk", '"': "rst"},
    )
    result = cmp(
        reference, candidate,
        [("top.clk", "top.clk"), ("top.rst", "top.rst")],
    )
    by_name = {p["candidate"]: p["mismatchFs"] for p in result["pairs"]}
    assert by_name == {"top.clk": 10_000_000, "top.rst": 5_000_000}
    assert result["mismatchFs"] == 15_000_000


def test_x_and_z_levels_are_compared():
    reference = build_vcd("#0\nx!\n#20\nx!\n", timescale="1ns")
    candidate = build_vcd("#0\nz!\n#20000\nz!\n", timescale="1ps")
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == [
        {"start": 0, "end": 20_000_000, "referenceValue": "x", "candidateValue": "z"}
    ]


def test_uninitialized_both_sides_is_x_and_equal():
    reference = build_vcd("#0\n0\"\n#20\n0\"\n")  # clk (!) never driven
    candidate = build_vcd("#0\n0\"\n#20000\n0\"\n", timescale="1ps")
    result = cmp(reference, candidate)
    assert result["pairs"][0]["differences"] == []


# --- error attribution ----------------------------------------------------


def test_reference_parse_error_is_attributed_to_reference():
    good = build_vcd("#0\n0!\n#20\n0!\n")
    bad = build_vcd("#0\n0!\n#10\nwhatisthis\n#20\n0!\n")
    with pytest.raises(VCDError) as exc:
        cmp(bad, good)
    assert exc.value.source == "reference"
    assert exc.value.code == "VCD_SYNTAX_ERROR"


def test_candidate_parse_error_is_attributed_to_candidate():
    good = build_vcd("#0\n0!\n#20\n0!\n")
    bad = build_vcd("#0\n0!\n#10\nwhatisthis\n#20\n0!\n")
    with pytest.raises(VCDError) as exc:
        cmp(good, bad)
    assert exc.value.source == "candidate"
    assert exc.value.code == "VCD_SYNTAX_ERROR"


def test_missing_reference_signal_is_attributed_to_reference():
    good = build_vcd("#0\n0!\n#20\n0!\n")
    with pytest.raises(VCDError) as exc:
        cmp(good, good, [("top.absent", "top.clk")])
    assert exc.value.source == "reference"
    assert exc.value.code == "UNDECLARED_SIGNAL"


def test_missing_candidate_signal_is_attributed_to_candidate():
    good = build_vcd("#0\n0!\n#20\n0!\n")
    with pytest.raises(VCDError) as exc:
        cmp(good, good, [("top.clk", "top.absent")])
    assert exc.value.source == "candidate"
    assert exc.value.code == "UNDECLARED_SIGNAL"


def test_uncovered_window_reference_side():
    reference = build_vcd("#0\n0!\n#10\n1!\n")
    candidate = build_vcd("#0\n0!\n#20000\n1!\n", timescale="1ps")
    with pytest.raises(VCDError) as exc:
        cmp(reference, candidate)
    assert exc.value.source == "reference"
    assert exc.value.code == "WINDOW_NOT_COVERED"


def test_uncovered_window_candidate_side():
    reference = build_vcd("#0\n0!\n#20\n1!\n")
    candidate = build_vcd("#0\n0!\n#10000\n1!\n", timescale="1ps")
    with pytest.raises(VCDError) as exc:
        cmp(reference, candidate)
    assert exc.value.source == "candidate"
    assert exc.value.code == "WINDOW_NOT_COVERED"


def test_no_partial_results_when_candidate_fails_late():
    # Reference is valid for the window; candidate has a parse error near EOF.
    reference = build_vcd("#0\n0!\n#20\n1!\n")
    candidate = build_vcd("#0\n0!\n#19000\n1!\n#20000\n2!\n", timescale="1ps")
    with pytest.raises(VCDError) as exc:
        cmp(reference, candidate)
    assert exc.value.source == "candidate"


def test_combined_change_budget_is_comparison_error():
    from app.vcd import MAX_VALUE_CHANGES

    half = MAX_VALUE_CHANGES // 2 + 1
    reference = build_vcd(
        "\n".join(f"#{i}\n{'1' if i % 2 else '0'}!\n" for i in range(half)),
        timescale="1ns",
    )
    candidate = build_vcd(
        "\n".join(f"#{i}\n{'1' if i % 2 else '0'}!\n" for i in range(half)),
        timescale="1ps",
    )
    window = Window(0, half * 1_000_000)
    with pytest.raises(VCDError) as exc:
        cmp(reference, candidate, PAIR, window)
    assert exc.value.code == "TOO_MANY_CHANGES"
    assert exc.value.source == "comparison"


def test_combined_change_budget_at_limit_passes():
    from app.vcd import MAX_VALUE_CHANGES

    half = MAX_VALUE_CHANGES // 2
    reference = build_vcd(
        "\n".join(f"#{i}\n0!\n" for i in range(half)), timescale="1ns"
    )
    # 1ps trace with timestamps x1000 so it spans the same fs window.
    candidate = build_vcd(
        "\n".join(f"#{i * 1000}\n0!\n" for i in range(half)), timescale="1ps"
    )
    window = Window(0, max(1, half - 1) * 1_000_000)
    result = cmp(reference, candidate, PAIR, window)
    assert result["pairs"][0]["mismatchFs"] == 0


# --- pair parsing ---------------------------------------------------------


def test_parse_pairs_repeated_fields():
    raw = [
        json.dumps({"reference": "top.a", "candidate": "top.b"}),
        json.dumps({"reference": "top.c", "candidate": "top.d"}),
    ]
    pairs = parse_pairs(raw)
    assert [(p.reference, p.candidate) for p in pairs] == [
        ("top.a", "top.b"), ("top.c", "top.d")
    ]


def test_parse_pairs_json_array():
    raw = [json.dumps([
        {"reference": "top.a", "candidate": "top.b"},
        {"reference": "top.c", "candidate": "top.d"},
    ])]
    pairs = parse_pairs(raw)
    assert len(pairs) == 2


def test_parse_pairs_requires_at_least_one():
    with pytest.raises(VCDError) as exc:
        parse_pairs([])
    assert exc.value.code == "MISSING_FIELD"


def test_parse_pairs_rejects_empty_array():
    with pytest.raises(VCDError) as exc:
        parse_pairs(["[]"])
    assert exc.value.code == "INVALID_PAIR"


def test_parse_pairs_rejects_malformed_entry():
    with pytest.raises(VCDError) as exc:
        parse_pairs(["not-json"])
    assert exc.value.code == "INVALID_JSON"
    with pytest.raises(VCDError) as exc:
        parse_pairs([json.dumps({"reference": "top.a"})])
    assert exc.value.code == "INVALID_PAIR"
    with pytest.raises(VCDError) as exc:
        parse_pairs([json.dumps(["top.a", "top.b"])])
    assert exc.value.code == "INVALID_PAIR"


def test_parse_pairs_rejects_duplicates():
    raw = [
        json.dumps({"reference": "top.a", "candidate": "top.b"}),
        json.dumps({"reference": "top.a", "candidate": "top.b"}),
    ]
    with pytest.raises(VCDError) as exc:
        parse_pairs(raw)
    assert exc.value.code == "DUPLICATE_PAIR"


def test_parse_pairs_limit_is_64():
    assert MAX_PAIRS == 64
    raw = [
        json.dumps({"reference": f"top.a{i}", "candidate": f"top.b{i}"})
        for i in range(MAX_PAIRS)
    ]
    assert len(parse_pairs(raw)) == MAX_PAIRS
    raw.append(json.dumps({"reference": "one.too", "candidate": "many.now"}))
    with pytest.raises(VCDError) as exc:
        parse_pairs(raw)
    assert exc.value.code == "TOO_MANY_PAIRS"
