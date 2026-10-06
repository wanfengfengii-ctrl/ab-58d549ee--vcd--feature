"""HTTP-level tests for POST /api/vcd/compare."""

from __future__ import annotations

import io
import json

from app.server import create_app
from tests.conftest import build_vcd


def client():
    return create_app().test_client()


def post_compare(reference, candidate, pairs, start=0, end=20_000_000,
                 pair_field="pair", window=None, extra_files=None, omit=()):
    data = {}
    if "reference" not in omit:
        ref_bytes = reference.encode("ascii") if isinstance(reference, str) else reference
        data["reference"] = (io.BytesIO(ref_bytes), "reference.vcd")
    if "candidate" not in omit:
        cand_bytes = candidate.encode("ascii") if isinstance(candidate, str) else candidate
        data["candidate"] = (io.BytesIO(cand_bytes), "candidate.vcd")
    data["window"] = json.dumps(window or {"start": start, "end": end})
    if pairs is not None:
        if isinstance(pairs, str):
            data[pair_field] = pairs
        else:
            data[pair_field] = [json.dumps(p) for p in pairs]
    if extra_files:
        data.update(extra_files)
    return client().post(
        "/api/vcd/compare", data=data, content_type="multipart/form-data"
    )


REF_NS = build_vcd("#0\n0!\n#10\n1!\n#20\n0!\n", timescale="1ns")
CAND_PS_EQ = build_vcd("#0\n0!\n#10000\n1!\n#20000\n0!\n", timescale="1ps")
CAND_PS_DIFF = build_vcd(
    "#0\n0!\n#10000\n1!\n#15000\n0!\n#20000\n0!\n", timescale="1ps"
)
PAIR = [{"reference": "top.clk", "candidate": "top.clk"}]


def test_compare_identical_cross_timescale():
    resp = post_compare(REF_NS, CAND_PS_EQ, PAIR)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["window"] == {"start": 0, "end": 20_000_000, "unit": "fs"}
    assert body["pairs"] == [
        {
            "reference": "top.clk",
            "candidate": "top.clk",
            "differences": [],
            "mismatchFs": 0,
        }
    ]
    assert body["mismatchFs"] == 0


def test_compare_reports_real_divergence():
    resp = post_compare(REF_NS, CAND_PS_DIFF, PAIR)
    assert resp.status_code == 200
    pair = resp.get_json()["pairs"][0]
    assert pair["differences"] == [
        {
            "start": 15_000_000,
            "end": 20_000_000,
            "referenceValue": "1",
            "candidateValue": "0",
        }
    ]
    assert pair["mismatchFs"] == 5_000_000


def test_pairs_as_single_json_array_field():
    raw = json.dumps(PAIR)
    resp = post_compare(REF_NS, CAND_PS_EQ, raw)
    assert resp.status_code == 200
    assert resp.get_json()["pairs"][0]["mismatchFs"] == 0


def test_pair_order_is_preserved():
    vcd = build_vcd(
        "#0\n0!\n0\"\n#20\n1!\n1\"\n",
        signals={"!": "clk", '"': "rst"},
    )
    pairs = [
        {"reference": "top.rst", "candidate": "top.rst"},
        {"reference": "top.clk", "candidate": "top.clk"},
    ]
    resp = post_compare(vcd, vcd, pairs)
    assert resp.status_code == 200
    assert [p["reference"] for p in resp.get_json()["pairs"]] == [
        "top.rst", "top.clk"
    ]


def test_missing_reference_field():
    resp = post_compare(REF_NS, CAND_PS_EQ, PAIR, omit=("reference",))
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_FIELD"
    assert err["source"] == "reference"


def test_missing_candidate_field():
    resp = post_compare(REF_NS, CAND_PS_EQ, PAIR, omit=("candidate",))
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_FIELD"
    assert err["source"] == "candidate"


def test_missing_window_is_comparison_error():
    data = {
        "reference": (io.BytesIO(REF_NS.encode()), "r.vcd"),
        "candidate": (io.BytesIO(CAND_PS_EQ.encode()), "c.vcd"),
        "pair": [json.dumps(PAIR[0])],
    }
    resp = client().post(
        "/api/vcd/compare", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_FIELD"
    assert err["source"] == "comparison"


def test_missing_pairs_is_comparison_error():
    resp = post_compare(REF_NS, CAND_PS_EQ, None)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "MISSING_FIELD"
    assert err["source"] == "comparison"


def test_duplicate_pair_is_comparison_error():
    resp = post_compare(REF_NS, CAND_PS_EQ, PAIR + PAIR)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "DUPLICATE_PAIR"
    assert err["source"] == "comparison"
    assert "pairs" not in resp.get_json()


def test_too_many_pairs():
    pairs = [
        {"reference": f"top.a{i}", "candidate": f"top.b{i}"} for i in range(65)
    ]
    resp = post_compare(REF_NS, CAND_PS_EQ, pairs)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "TOO_MANY_PAIRS"
    assert err["source"] == "comparison"


def test_reference_syntax_error_attributed():
    bad = build_vcd("#0\n0!\n#10\nwhatisthis\n#20\n0!\n")
    resp = post_compare(bad, CAND_PS_EQ, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VCD_SYNTAX_ERROR"
    assert err["source"] == "reference"
    assert isinstance(err.get("line"), int)
    assert "pairs" not in resp.get_json()


def test_candidate_syntax_error_attributed():
    bad = build_vcd("#0\n0!\n#10000\nwhatisthis\n#20000\n0!\n", timescale="1ps")
    resp = post_compare(REF_NS, bad, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "VCD_SYNTAX_ERROR"
    assert err["source"] == "candidate"


def test_missing_reference_signal_attributed():
    resp = post_compare(REF_NS, CAND_PS_EQ,
                        [{"reference": "top.nope", "candidate": "top.clk"}])
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert err["source"] == "reference"


def test_missing_candidate_signal_attributed():
    resp = post_compare(REF_NS, CAND_PS_EQ,
                        [{"reference": "top.clk", "candidate": "top.nope"}])
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "UNDECLARED_SIGNAL"
    assert err["source"] == "candidate"


def test_reference_window_not_covered():
    short_ref = build_vcd("#0\n0!\n#10\n1!\n", timescale="1ns")
    resp = post_compare(short_ref, CAND_PS_EQ, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "WINDOW_NOT_COVERED"
    assert err["source"] == "reference"


def test_candidate_window_not_covered():
    short_cand = build_vcd("#0\n0!\n#10000\n1!\n", timescale="1ps")
    resp = post_compare(REF_NS, short_cand, PAIR)
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "WINDOW_NOT_COVERED"
    assert err["source"] == "candidate"


def test_reference_too_large_attributed():
    from app.vcd import MAX_FILE_BYTES

    padding = "$comment " + ("x " * (2 * 1024 * 1024 + 10)) + " $end\n"
    big = build_vcd(padding + "#0\n0!\n")
    assert len(big.encode()) > MAX_FILE_BYTES
    resp = post_compare(big, CAND_PS_EQ, PAIR)
    assert resp.status_code == 413
    assert resp.get_json()["error"]["source"] == "reference"


def test_candidate_non_ascii_attributed():
    raw = CAND_PS_EQ.encode() + b"\xff\xfe"
    resp = post_compare(REF_NS, raw, PAIR)
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert err["code"] == "NOT_ASCII"
    assert err["source"] == "candidate"


def test_window_endpoint_contract_unchanged():
    # The window error body must not gain a "source" field.
    data = {
        "file": (io.BytesIO(REF_NS.encode()), "w.vcd"),
        "window": json.dumps({"start": 0, "end": 999_000_000}),
        "signals": ["top.clk"],
    }
    resp = client().post(
        "/api/vcd/window", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 422
    err = resp.get_json()["error"]
    assert err["code"] == "WINDOW_NOT_COVERED"
    assert "source" not in err
