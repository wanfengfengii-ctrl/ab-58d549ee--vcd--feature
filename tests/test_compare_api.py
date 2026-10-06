"""HTTP-level tests for POST /api/vcd/compare."""

from __future__ import annotations

import io
import json

from app.server import create_app
from tests.conftest import build_vcd


def client():
    return create_app().test_client()


def post_compare(cl, ref_text, cand_text, pairs, start=0, end=20_000_000):
    if isinstance(pairs, str):
        pairs_field = pairs
    else:
        pairs_field = json.dumps(pairs)
    data = {
        "reference": (io.BytesIO(ref_text.encode("ascii")), "ref.vcd"),
        "candidate": (io.BytesIO(cand_text.encode("ascii")), "cand.vcd"),
        "window": json.dumps({"start": start, "end": end}),
        "pairs": pairs_field,
    }
    return cl.post(
        "/api/vcd/compare",
        data=data,
        content_type="multipart/form-data",
    )


REF_NS = build_vcd(
    "#0\n0!\n#5\n1!\n#10\n0!\n#15\n1!\n#20\n0!\n",
    timescale="1ns",
)
CAND_PS = build_vcd(
    "#0\n0!\n#5000\n1!\n#10000\n0!\n#15000\n1!\n#20000\n0!\n",
    timescale="1ps",
)
PAIR = [{"index": 0, "reference": "top.clk", "candidate": "top.clk"}]


def test_compare_identical_cross_timescale():
    resp = post_compare(client(), REF_NS, CAND_PS, PAIR)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["window"] == {"start": 0, "end": 20_000_000, "unit": "fs"}
    pair = body["pairs"][0]
    assert pair["mismatches"] == []
    assert pair["totalMismatchFs"] == 0
    assert pair["index"] == 0


def test_compare_reports_mismatch_interval_levels_and_fs():
    cand = build_vcd(
        "#0\n0!\n#5\n1!\n#12\n0!\n#15\n1!\n#20\n0!\n",
        timescale="1ns",
    )
    resp = post_compare(client(), REF_NS, cand, PAIR)
    assert resp.status_code == 200
    pair = resp.get_json()["pairs"][0]
    assert pair["mismatches"] == [
        {
            "start": 10_000_000,
            "end": 12_000_000,
            "reference": "0",
            "candidate": "1",
            "mismatchFs": 2_000_000,
        }
    ]
    assert pair["totalMismatchFs"] == 2_000_000


def test_compare_preserves_pair_order_and_echoes_names():
    pairs = [
        {"index": 9, "reference": "top.data", "candidate": "top.data"},
        {"index": 1, "reference": "top.clk", "candidate": "top.clk"},
    ]
    resp = post_compare(client(), REF_NS, CAND_PS, pairs)
    assert resp.status_code == 200
    body = resp.get_json()
    assert [p["index"] for p in body["pairs"]] == [9, 1]
    assert body["pairs"][0]["reference"] == "top.data"


def test_compare_repeated_pair_form_fields():
    data = {
        "reference": (io.BytesIO(REF_NS.encode()), "ref.vcd"),
        "candidate": (io.BytesIO(CAND_PS.encode()), "cand.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "pairs": [
            json.dumps({"index": 0, "reference": "top.clk", "candidate": "top.clk"}),
            json.dumps({"index": 1, "reference": "top.data", "candidate": "top.data"}),
        ],
    }
    resp = client().post(
        "/api/vcd/compare", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    assert [p["index"] for p in resp.get_json()["pairs"]] == [0, 1]


def test_missing_reference_file():
    cl = client()
    data = {
        "candidate": (io.BytesIO(CAND_PS.encode()), "cand.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "pairs": json.dumps(PAIR),
    }
    resp = cl.post("/api/vcd/compare", data=data,
                   content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_missing_candidate_file():
    cl = client()
    data = {
        "reference": (io.BytesIO(REF_NS.encode()), "ref.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "pairs": json.dumps(PAIR),
    }
    resp = cl.post("/api/vcd/compare", data=data,
                   content_type="multipart/form-data")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "MISSING_FIELD"


def test_missing_pairs_field():
    cl = client()
    data = {
        "reference": (io.BytesIO(REF_NS.encode()), "ref.vcd"),
        "candidate": (io.BytesIO(CAND_PS.encode()), "cand.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
    }
    resp = cl.post("/api/vcd/compare", data=data,
                   content_type="multipart/form-data")
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_FIELD"
    assert err.get("side") == "comparison"


def test_duplicate_pair_index_is_comparison_error():
    pairs = [
        {"index": 0, "reference": "top.clk", "candidate": "top.clk"},
        {"index": 0, "reference": "top.data", "candidate": "top.data"},
    ]
    resp = post_compare(client(), REF_NS, CAND_PS, pairs)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "DUPLICATE_PAIR"
    assert err["side"] == "comparison"
    assert "pairs" not in resp.get_json()


def test_too_many_pairs():
    pairs = [
        {"index": i, "reference": "top.clk", "candidate": "top.clk"}
        for i in range(65)
    ]
    resp = post_compare(client(), REF_NS, CAND_PS, pairs)
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "INVALID_PAIRS"


def test_reference_parse_error_is_tagged_reference():
    bad = build_vcd("#10\n0!\n#5\n1!\n")
    resp = post_compare(client(), bad, CAND_PS, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "TIME_REGRESSION"
    assert err["side"] == "reference"
    assert "pairs" not in resp.get_json()


def test_candidate_parse_error_is_tagged_candidate():
    bad = build_vcd("#10\n0!\n#5\n1!\n")
    resp = post_compare(client(), REF_NS, bad, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "TIME_REGRESSION"
    assert err["side"] == "candidate"


def test_missing_signal_tagged_with_side():
    resp = post_compare(
        client(), REF_NS, CAND_PS,
        [{"index": 0, "reference": "top.ghost", "candidate": "top.clk"}],
    )
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert err["side"] == "reference"

    resp = post_compare(
        client(), REF_NS, CAND_PS,
        [{"index": 0, "reference": "top.clk", "candidate": "top.ghost"}],
    )
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert err["side"] == "candidate"


def test_uncovered_window_tagged_with_side():
    short = build_vcd("#0\n0!\n#10\n1!\n", timescale="1ns")
    resp = post_compare(client(), short, CAND_PS, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "WINDOW_NOT_COVERED"
    assert err["side"] == "reference"

    resp = post_compare(client(), REF_NS, short, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["side"] == "candidate"


def test_invalid_window_is_comparison_error():
    resp = post_compare(client(), REF_NS, CAND_PS, PAIR, start=10, end=5)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "INVALID_WINDOW"
    assert err["side"] == "comparison"


def test_non_ascii_reference_tagged_reference():
    raw = REF_NS.encode() + b"\xff"
    cl = client()
    data = {
        "reference": (io.BytesIO(raw), "ref.vcd"),
        "candidate": (io.BytesIO(CAND_PS.encode()), "cand.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "pairs": json.dumps(PAIR),
    }
    resp = cl.post("/api/vcd/compare", data=data,
                   content_type="multipart/form-data")
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "NOT_ASCII"
    assert err["side"] == "reference"


def test_window_endpoint_contract_unchanged():
    # The existing single-file endpoint keeps its field names and shape.
    data = {
        "file": (io.BytesIO(REF_NS.encode()), "wave.vcd"),
        "window": json.dumps({"start": 0, "end": 20_000_000}),
        "signals": json.dumps(["top.clk"]),
    }
    resp = client().post("/api/vcd/window", data=data,
                         content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"window", "signals"}
    assert "top.clk" in body["signals"]
