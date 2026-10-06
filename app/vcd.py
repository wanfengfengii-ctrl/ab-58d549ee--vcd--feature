"""VCD (Value Change Dump) parsing and half-open window extraction.

Only the strict subset required by the verification platform is accepted:

* 1-bit ``wire`` declarations with canonical scope nesting
* timescales of 1/10/100 fs|ps|ns
* non-decreasing timestamps
* at most 50 000 value changes per request

The parser is a *total* function: it either returns a complete result or
raises :class:`VCDError` with a locatable reason. No partial results are
ever produced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

# --- limits ---------------------------------------------------------------

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_VALUE_CHANGES = 50_000
MAX_FS = 2**63 - 1

SCOPE_KINDS = {"module", "begin", "fork", "function", "task"}
DUMP_KEYWORDS = {
    "$dumpvars",
    "$dumpports",
    "$dumpall",
    "$dumpon",
    "$dumpoff",
}
HEADER_WORDS = {
    "$comment",
    "$date",
    "$version",
}
UNIT_FS = {"fs": 1, "ps": 1_000, "ns": 1_000_000}
TIME_NUMBERS = {"1", "10", "100"}
VALUE_CHARS = set("01xXzZ")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
INT_RE = re.compile(r"^(0|[1-9][0-9]*)$")

SCALAR_VALUES = ("0", "1", "x", "z")


class VCDError(Exception):
    """A parse or semantic error that can be reported to the API client."""

    def __init__(
        self,
        code: str,
        message: str,
        line: Optional[int] = None,
        details: Optional[dict] = None,
        http_status: int = 422,
        side: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.details = details
        self.http_status = http_status
        self.side = side

    def to_response(self) -> tuple[dict, int]:
        err: dict = {"code": self.code, "message": self.message}
        if self.side is not None:
            err["side"] = self.side
        if self.line is not None:
            err["line"] = self.line
        details = dict(self.details or {})
        if self.side is not None:
            details.setdefault("side", self.side)
        if details:
            err["details"] = details
        return {"error": err}, self.http_status


def bad_request(code: str, message: str, details: Optional[dict] = None) -> VCDError:
    return VCDError(code, message, details=details, http_status=400)


# --- parsed model ---------------------------------------------------------


def merge_segments(
    segments: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    """Merge adjacent segments carrying the same value."""
    merged: list[tuple[int, int, str]] = []
    for seg_start, seg_end, value in segments:
        if merged and merged[-1][2] == value and merged[-1][1] == seg_start:
            merged[-1] = (merged[-1][0], seg_end, value)
        else:
            merged.append((seg_start, seg_end, value))
    return merged


@dataclass
class VCD:
    timescale_fs: int
    names: dict[str, str]                      # full hierarchical name -> id
    changes: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    change_count: int = 0

    def signal_timeline(self, signal: str, start: int, end: int) -> list[dict]:
        """Return merged half-open [start, end) intervals for one signal."""
        ident = self.names[signal]
        events = self.changes.get(ident, [])

        # Effective value at the window start: last change with t <= start.
        # Uninitialized simulated nets read as x.
        current = "x"
        # (time, final value) for strictly-increasing interior boundaries.
        boundary_values: list[tuple[int, str]] = []
        for t, value in events:
            if t <= start:
                current = value
            elif t < end:
                if boundary_values and boundary_values[-1][0] == t:
                    # Same-timestamp assignments: text order arbitrates,
                    # so the final assignment at that time is the value.
                    boundary_values[-1] = (t, value)
                else:
                    boundary_values.append((t, value))
            else:
                break

        raw: list[tuple[int, int, str]] = []
        prev_t, prev_v = start, current
        for t, value in boundary_values:
            raw.append((prev_t, t, prev_v))
            prev_t, prev_v = t, value
        raw.append((prev_t, end, prev_v))

        return [
            {"start": s, "end": e, "value": v}
            for s, e, v in merge_segments(raw)
        ]

    def covers_window(self, start: int, end: int) -> bool:
        """Whether [start, end) is fully interpretable from this waveform."""
        assert self.first_ts is not None and self.last_ts is not None
        return start >= self.first_ts and end <= self.last_ts


# --- tokenizer ------------------------------------------------------------


def _tokenize(text: str) -> list[tuple[str, int]]:
    """Split on whitespace while keeping the 1-based line of each token."""
    tokens: list[tuple[str, int]] = []
    line = 1
    pos = 0
    for match in re.finditer(r"\S+", text):
        line += text.count("\n", pos, match.start())
        pos = match.end()
        tokens.append((match.group(), line))
    return tokens


# --- parser ---------------------------------------------------------------


def parse(text: str, change_budget: int = MAX_VALUE_CHANGES) -> VCD:
    """Parse one VCD, accepting at most *change_budget* value changes.

    The budget lets a two-file compare enforce the per-request cap of
    50 000 changes across both files combined.
    """
    tokens = _tokenize(text)
    vcd = VCD(timescale_fs=0, names={})
    id_kind: dict[str, tuple[str, str]] = {}
    declared_ids: set[str] = set()
    scope_stack: list[str] = []
    used_local_names: set[str] = set()  # sibling scopes/signals at one path

    def err(code: str, msg: str, line: int) -> VCDError:
        return VCDError(code, msg, line=line)

    def take(i: int) -> tuple[str, int]:
        if i >= len(tokens):
            raise VCDError("VCD_SYNTAX_ERROR", "unexpected end of file")
        return tokens[i]

    # ---- header section ---------------------------------------------------
    i = 0
    timescale_seen = False
    definitions_closed = False

    while i < len(tokens):
        tok, line = tokens[i]

        if tok in HEADER_WORDS:
            depth_end = i + 1
            while depth_end < len(tokens) and tokens[depth_end][0] != "$end":
                depth_end += 1
            if depth_end >= len(tokens):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"{tok} section is missing a closing $end",
                    line,
                )
            i = depth_end + 1
            continue

        if tok == "$timescale":
            if timescale_seen:
                raise err("VCD_SYNTAX_ERROR", "duplicate $timescale", line)

            def finish_timescale(number: str, unit: str, at_line: int) -> None:
                if number not in TIME_NUMBERS or unit not in UNIT_FS:
                    raise VCDError(
                        "UNSUPPORTED_TIMESCALE",
                        f"unsupported timescale {number} {unit}: "
                        "only 1, 10 or 100 times fs, ps or ns are accepted",
                        line=at_line,
                    )
                vcd.timescale_fs = int(number) * UNIT_FS[unit]

            # Accepted forms: "$timescale 1 ns $end" and "$timescale 1ns $end".
            nxt, nxt_line = take(i + 1)
            fused = re.match(r"^(\d+)([a-zA-Z]+)$", nxt)
            if fused:
                end_tok, _ = take(i + 2)
                if end_tok != "$end":
                    raise err("VCD_SYNTAX_ERROR", "malformed $timescale", line)
                finish_timescale(fused.group(1), fused.group(2), nxt_line)
                i += 3
            else:
                if nxt not in TIME_NUMBERS:
                    raise VCDError(
                        "UNSUPPORTED_TIMESCALE",
                        f"unsupported timescale {nxt!r}: only 1, 10 or 100 "
                        "times fs, ps or ns are accepted",
                        line=nxt_line,
                    )
                unit_tok, unit_line = take(i + 2)
                end_tok, _ = take(i + 3)
                if end_tok != "$end":
                    raise err("VCD_SYNTAX_ERROR", "malformed $timescale", line)
                finish_timescale(nxt, unit_tok, unit_line)
                i += 4
            timescale_seen = True
            continue

        if tok == "$scope":
            if i + 4 > len(tokens) or tokens[i + 3][0] != "$end":
                raise err("VCD_SYNTAX_ERROR", "malformed $scope", line)
            kind, name = tokens[i + 1][0], tokens[i + 2][0]
            if kind not in SCOPE_KINDS:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"non-canonical scope kind {kind!r}",
                    tokens[i + 1][1],
                )
            if not NAME_RE.match(name):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"non-canonical scope name {name!r}",
                    tokens[i + 2][1],
                )
            scope_stack.append(name)
            full = ".".join(scope_stack)
            if full in used_local_names:
                raise err(
                    "DUPLICATE_NAME",
                    f"scope {full!r} is declared more than once",
                    tokens[i + 2][1],
                )
            used_local_names.add(full)
            i += 4
            continue

        if tok == "$upscope":
            if i + 1 >= len(tokens) or tokens[i + 1][0] != "$end":
                raise err("VCD_SYNTAX_ERROR", "malformed $upscope", line)
            if not scope_stack:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "$upscope without a matching $scope",
                    line,
                )
            scope_stack.pop()
            i += 2
            continue

        if tok == "$var":
            # $var <kind> <width> <id> <name> [<msb:lsb>] $end
            j = i + 1
            parts: list[tuple[str, int]] = []
            while j < len(tokens) and tokens[j][0] != "$end":
                parts.append(tokens[j])
                j += 1
            if j >= len(tokens):
                raise err("VCD_SYNTAX_ERROR", "$var is missing $end", line)
            if len(parts) not in (4, 5):
                raise err("VCD_SYNTAX_ERROR", "malformed $var declaration", line)
            kind, width, ident, name = parts[0][0], parts[1][0], parts[2][0], parts[3][0]
            if kind != "wire":
                raise VCDError(
                    "UNSUPPORTED_VAR",
                    f"only 1-bit wire is accepted, got {kind!r}",
                    line=parts[0][1],
                )
            if width != "1":
                raise VCDError(
                    "UNSUPPORTED_VAR",
                    f"only 1-bit wire is accepted, got width {width!r}",
                    line=parts[1][1],
                )
            if not NAME_RE.match(name):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"non-canonical signal name {name!r}",
                    parts[3][1],
                )
            if not scope_stack:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "$var must be declared inside a $scope",
                    line,
                )
            if len(parts) == 5 and not re.match(r"^\[\d+(:\d+)?\]$", parts[4][0]):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"malformed vector bounds {parts[4][0]!r}",
                    parts[4][1],
                )
            bounds = parts[4][0] if len(parts) == 5 else ""
            if bounds and bounds not in ("[0]", "[0:0]"):
                raise VCDError(
                    "UNSUPPORTED_VAR",
                    f"1-bit wire cannot carry bounds {bounds!r}",
                    line=parts[4][1],
                )
            if ident in id_kind and id_kind[ident] != (kind, width):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"identifier {ident!r} redeclared with a different kind/width",
                    parts[2][1],
                )
            id_kind.setdefault(ident, (kind, width))
            declared_ids.add(ident)
            full_name = ".".join(scope_stack + [name + bounds])
            if full_name in vcd.names or full_name in used_local_names:
                raise err(
                    "DUPLICATE_NAME",
                    f"signal {full_name!r} is declared more than once",
                    parts[3][1],
                )
            vcd.names[full_name] = ident
            vcd.changes.setdefault(ident, [])
            used_local_names.add(full_name)
            i = j + 1
            continue

        if tok == "$enddefinitions":
            if i + 1 >= len(tokens) or tokens[i + 1][0] != "$end":
                raise err("VCD_SYNTAX_ERROR", "malformed $enddefinitions", line)
            if not timescale_seen:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "$timescale must be declared before $enddefinitions",
                    line,
                )
            if scope_stack:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "unclosed scope before $enddefinitions",
                    line,
                )
            definitions_closed = True
            i += 2
            break

        raise err(
            "VCD_SYNTAX_ERROR",
            f"unexpected token {tok!r} in VCD header",
            line,
        )

    if not definitions_closed:
        raise VCDError(
            "VCD_SYNTAX_ERROR",
            "VCD header is missing $enddefinitions $end",
        )

    # ---- simulation section ----------------------------------------------
    current_time: Optional[int] = None
    prev_time: Optional[int] = None
    in_dump_block = False
    change_count = 0

    def parse_value_change(tok: str, line: int, idx: int) -> int:
        """Consume one value change starting at token index *idx*.

        Returns the index just past the consumed tokens.
        """
        nonlocal current_time, change_count

        if current_time is None:
            raise err(
                "VCD_SYNTAX_ERROR",
                "value change appears before any timestamp",
                line,
            )

        first = tok[0]

        if first in "rR":
            raise VCDError(
                "UNSUPPORTED_VAR",
                "real value changes are not accepted for 1-bit wire",
                line=line,
            )

        if first in "bB":
            # b<bits> <id>: either fused as "b<bits>" followed by the id,
            # or a bare "b" followed by bits and id.
            body = tok[1:]
            if body:
                bits = body
            else:
                bits, line = take(idx + 1)
                idx += 1
            ident, _ = take(idx + 1)
            idx += 1
            if len(bits) != 1 or bits not in VALUE_CHARS:
                raise VCDError(
                    "UNSUPPORTED_VAR",
                    f"1-bit wire cannot take vector value {('b' + bits)!r}",
                    line=line,
                )
            value = bits.lower()
        elif first in "01xXzZ":
            value = first.lower()
            if len(tok) > 1:
                ident = tok[1:]  # fused form, e.g. 0!
            else:
                ident, _ = take(idx + 1)
                idx += 1
        else:
            raise err(
                "VCD_SYNTAX_ERROR",
                f"unrecognized token {tok!r} in simulation section",
                line,
            )

        if ident not in declared_ids:
            raise VCDError(
                "UNDECLARED_IDENTIFIER",
                f"value change references undeclared identifier {ident!r}",
                line=line,
            )

        change_count += 1
        if change_count > change_budget:
            raise VCDError(
                "TOO_MANY_CHANGES",
                f"more than {MAX_VALUE_CHANGES} value changes in one request",
                line=line,
            )

        vcd.changes[ident].append((current_time, value))
        return idx

    while i < len(tokens):
        tok, line = tokens[i]

        if tok.startswith("#"):
            if in_dump_block:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "timestamp is not allowed inside a $dump block",
                    line,
                )
            raw = tok[1:]
            if not INT_RE.match(raw):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"invalid timestamp {tok!r}",
                    line,
                )
            t = int(raw) * vcd.timescale_fs
            if t > MAX_FS:
                raise VCDError(
                    "OUT_OF_RANGE",
                    f"timestamp {raw} ({raw} x {vcd.timescale_fs} fs) "
                    "exceeds the supported femtosecond range",
                    line=line,
                )
            if prev_time is not None and t < prev_time:
                raise VCDError(
                    "TIME_REGRESSION",
                    f"timestamp goes backwards: {t} fs after {prev_time} fs",
                    line=line,
                    details={"previous_fs": prev_time, "timestamp_fs": t},
                )
            if vcd.first_ts is None:
                vcd.first_ts = t
            vcd.last_ts = t
            prev_time = t
            current_time = t
            i += 1
            continue

        if tok == "$comment":
            depth_end = i + 1
            while depth_end < len(tokens) and tokens[depth_end][0] != "$end":
                depth_end += 1
            if depth_end >= len(tokens):
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "$comment section is missing a closing $end",
                    line,
                )
            i = depth_end + 1
            continue

        if tok in DUMP_KEYWORDS:
            if in_dump_block:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    f"nested dump block {tok}",
                    line,
                )
            in_dump_block = True
            i += 1
            continue

        if tok == "$end":
            if not in_dump_block:
                raise err(
                    "VCD_SYNTAX_ERROR",
                    "unmatched $end in simulation section",
                    line,
                )
            in_dump_block = False
            i += 1
            continue

        i = parse_value_change(tok, line, i) + 1

    if in_dump_block:
        raise VCDError(
            "VCD_SYNTAX_ERROR",
            "dump block is missing its closing $end",
        )

    if vcd.first_ts is None:
        raise VCDError(
            "WINDOW_NOT_COVERED",
            "VCD contains no timestamps; no window can be interpreted",
        )

    vcd.change_count = change_count
    return vcd


# --- request level processing --------------------------------------------


@dataclass
class Window:
    start: int
    end: int


def parse_window(raw: Optional[str]) -> Window:
    if raw is None:
        raise bad_request("MISSING_FIELD", "form field 'window' is required")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise bad_request(
            "INVALID_JSON", f"'window' must be JSON: {exc.msg}"
        ) from exc
    if not isinstance(data, dict) or "start" not in data or "end" not in data:
        raise bad_request(
            "INVALID_WINDOW", "'window' must be an object with 'start' and 'end'"
        )
    start, end = data["start"], data["end"]
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) \
            or not isinstance(end, int):
        raise bad_request("INVALID_WINDOW", "window bounds must be integers")
    if start < 0 or end < 0 or end > MAX_FS:
        raise bad_request(
            "OUT_OF_RANGE",
            f"window bounds must lie within [0, {MAX_FS}] femtoseconds",
        )
    if start >= end:
        raise bad_request(
            "INVALID_WINDOW",
            "window must be half-open with start < end",
            details={"start": start, "end": end},
        )
    return Window(start=start, end=end)


def parse_signals(raw_list: list[str]) -> list[str]:
    if not raw_list:
        raise bad_request("MISSING_FIELD", "at least one signal must be selected")
    signals: list[str]
    if len(raw_list) == 1 and raw_list[0].lstrip().startswith("["):
        try:
            parsed = json.loads(raw_list[0])
        except json.JSONDecodeError as exc:
            raise bad_request("INVALID_JSON", f"'signals' must be JSON: {exc.msg}") from exc
        if not isinstance(parsed, list) or not all(isinstance(s, str) for s in parsed):
            raise bad_request(
                "INVALID_SIGNALS", "'signals' must be a JSON array of strings"
            )
        signals = parsed
    else:
        signals = raw_list
    if not signals:
        raise bad_request("INVALID_SIGNALS", "at least one signal must be selected")
    if len(set(signals)) != len(signals):
        seen: set[str] = set()
        dupes: list[str] = []
        for sig in signals:
            if sig in seen and sig not in dupes:
                dupes.append(sig)
            seen.add(sig)
        raise VCDError(
            "DUPLICATE_SIGNAL",
            "duplicate signal names in the selection",
            details={"duplicates": dupes},
            http_status=400,
        )
    return signals


def process(text: str, signals: list[str], window: Window) -> dict:
    """Parse *text* and extract the window. All-or-nothing result."""
    vcd = parse(text)

    missing = [s for s in signals if s not in vcd.names]
    if missing:
        raise VCDError(
            "UNDECLARED_SIGNAL",
            "selected signal is not declared in the VCD",
            details={"signals": missing},
            http_status=400,
        )

    assert vcd.first_ts is not None and vcd.last_ts is not None
    if not vcd.covers_window(window.start, window.end):
        raise VCDError(
            "WINDOW_NOT_COVERED",
            "window cannot be fully interpreted from the waveform: the VCD "
            "must contain a timestamp at or before window start and at or "
            "after window end",
            details={
                "window_start_fs": window.start,
                "window_end_fs": window.end,
                "first_timestamp_fs": vcd.first_ts,
                "last_timestamp_fs": vcd.last_ts,
            },
        )

    result_signals = {}
    for name in sorted(signals):
        result_signals[name] = vcd.signal_timeline(name, window.start, window.end)

    return {
        "window": {"start": window.start, "end": window.end, "unit": "fs"},
        "signals": result_signals,
    }


# --- golden/candidate comparison -----------------------------------------

MAX_PAIRS = 64


@dataclass
class Pair:
    """One numbered, fully-qualified reference/candidate signal pairing."""

    index: int
    reference: str
    candidate: str


def comparison_error(code: str, message: str, details: Optional[dict] = None) -> VCDError:
    """A 400-level error attributed to the comparison request itself."""
    return VCDError(code, message, details=details, http_status=400, side="comparison")


def parse_pairs(raw_list: list[str]) -> list[Pair]:
    """Parse the 1..64 uniquely numbered, complete hierarchical signal pairs.

    Accepted form fields (either style, not both at once):

    * repeated ``pairs`` fields, each a JSON object
      ``{"index": <int>, "reference": "<name>", "candidate": "<name>"}``
    * a single ``pairs`` field carrying a JSON array of such objects
    """
    if not raw_list:
        raise comparison_error("MISSING_FIELD", "form field 'pairs' is required")

    if len(raw_list) == 1 and raw_list[0].lstrip()[:1] in ("[", "{"):
        try:
            parsed = json.loads(raw_list[0])
        except json.JSONDecodeError as exc:
            raise comparison_error(
                "INVALID_PAIRS", f"'pairs' must be JSON: {exc.msg}"
            ) from exc
        items = parsed if isinstance(parsed, list) else [parsed]
    else:
        items = []
        for raw in raw_list:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise comparison_error(
                    "INVALID_PAIRS",
                    f"each 'pairs' field must be a JSON object: {exc.msg}",
                ) from exc
            items.append(item)

    if not isinstance(items, list) or not items:
        raise comparison_error(
            "INVALID_PAIRS", "'pairs' must contain at least one signal pairing"
        )
    if len(items) > MAX_PAIRS:
        raise comparison_error(
            "INVALID_PAIRS",
            f"at most {MAX_PAIRS} signal pairings are accepted per request",
            details={"count": len(items), "max": MAX_PAIRS},
        )

    pairs: list[Pair] = []
    seen_indices: set[int] = set()
    for pos, item in enumerate(items):
        where = f"pairs[{pos}]"
        if not isinstance(item, dict):
            raise comparison_error(
                "INVALID_PAIRS", f"{where} must be an object"
            )
        index = item.get("index")
        ref = item.get("reference")
        cand = item.get("candidate")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise comparison_error(
                "INVALID_PAIRS",
                f"{where} requires a non-negative integer 'index'",
            )
        if not isinstance(ref, str) or not isinstance(cand, str) or not ref or not cand:
            raise comparison_error(
                "INVALID_PAIRS",
                f"{where} requires non-empty string 'reference' and 'candidate'",
            )
        if index in seen_indices:
            raise comparison_error(
                "DUPLICATE_PAIR",
                f"pair index {index} is used more than once",
                details={"index": index},
            )
        seen_indices.add(index)
        pairs.append(Pair(index=index, reference=ref, candidate=cand))
    return pairs


def _diff_segments(
    ref_segments: list[tuple[int, int, str]],
    cand_segments: list[tuple[int, int, str]],
) -> list[tuple[int, int, str, str]]:
    """Sweep two covers of the same window; yield differing half-open runs.

    Both inputs are merged partitions of [start, end); the sweep walks every
    boundary in either timeline, so a disagreement is reported only for the
    fs it actually persists. Adjacent runs with the same level pair are
    coalesced.
    """
    diffs: list[tuple[int, int, str, str]] = []
    ri = ci = 0
    window_end = ref_segments[-1][1]
    pos = ref_segments[0][0]
    while pos < window_end:
        rseg = ref_segments[ri]
        cseg = cand_segments[ci]
        nxt = min(rseg[1], cseg[1])
        if rseg[2] != cseg[2]:
            if diffs and diffs[-1][1] == pos and diffs[-1][2:] == (rseg[2], cseg[2]):
                diffs[-1] = (diffs[-1][0], nxt, rseg[2], cseg[2])
            else:
                diffs.append((pos, nxt, rseg[2], cseg[2]))
        pos = nxt
        if rseg[1] == pos:
            ri += 1
        if cseg[1] == pos:
            ci += 1
    return diffs


def compare(
    reference_text: str,
    candidate_text: str,
    pairs: list[Pair],
    window: Window,
) -> dict:
    """Compare golden vs candidate windows. All-or-nothing result.

    Errors are tagged with the offending side: ``reference`` or ``candidate``
    for file/parse/coverage/signal problems, ``comparison`` for malformed
    pairings.
    """
    def _parse_side(text: str, side: str, budget: int) -> VCD:
        try:
            return parse(text, change_budget=budget)
        except VCDError as exc:
            exc.side = side
            if exc.code == "TOO_MANY_CHANGES":
                # The cap is per request and shared by the two uploads.
                exc.details = {
                    "max_changes_per_request": MAX_VALUE_CHANGES,
                }
            raise

    ref = _parse_side(reference_text, "reference", MAX_VALUE_CHANGES)
    remaining = MAX_VALUE_CHANGES - ref.change_count
    cand = _parse_side(candidate_text, "candidate", remaining)

    missing_ref = sorted({p.reference for p in pairs if p.reference not in ref.names})
    missing_cand = sorted({p.candidate for p in pairs if p.candidate not in cand.names})
    if missing_ref:
        raise VCDError(
            "UNDECLARED_SIGNAL",
            "paired signal is not declared in the reference VCD",
            details={"signals": missing_ref},
            http_status=400,
            side="reference",
        )
    if missing_cand:
        raise VCDError(
            "UNDECLARED_SIGNAL",
            "paired signal is not declared in the candidate VCD",
            details={"signals": missing_cand},
            http_status=400,
            side="candidate",
        )

    def not_covered(vcd: VCD, side: str) -> VCDError:
        return VCDError(
            "WINDOW_NOT_COVERED",
            f"window cannot be fully interpreted from the {side} waveform: "
            "the VCD must contain a timestamp at or before window start and "
            "at or after window end",
            details={
                "window_start_fs": window.start,
                "window_end_fs": window.end,
                "first_timestamp_fs": vcd.first_ts,
                "last_timestamp_fs": vcd.last_ts,
            },
            side=side,
        )

    if not ref.covers_window(window.start, window.end):
        raise not_covered(ref, "reference")
    if not cand.covers_window(window.start, window.end):
        raise not_covered(cand, "candidate")

    results = []
    for pair in pairs:
        ref_cover = ref.signal_timeline(pair.reference, window.start, window.end)
        cand_cover = cand.signal_timeline(pair.candidate, window.start, window.end)
        ref_segments = [(s["start"], s["end"], s["value"]) for s in ref_cover]
        cand_segments = [(s["start"], s["end"], s["value"]) for s in cand_cover]

        diffs_raw = _diff_segments(ref_segments, cand_segments)
        mismatches = [
            {
                "start": s,
                "end": e,
                "reference": rv,
                "candidate": cv,
                "mismatchFs": e - s,
            }
            for s, e, rv, cv in diffs_raw
        ]
        results.append(
            {
                "index": pair.index,
                "reference": pair.reference,
                "candidate": pair.candidate,
                "mismatches": mismatches,
                "totalMismatchFs": sum(m["mismatchFs"] for m in mismatches),
            }
        )

    return {
        "window": {"start": window.start, "end": window.end, "unit": "fs"},
        "pairs": results,
    }
