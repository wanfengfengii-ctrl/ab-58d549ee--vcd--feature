"""Shared VCD fixture builders."""

from __future__ import annotations

HEADER = """$date
Mon Jan 1 00:00:00 2024
$end
$version
test-harness
$end
$comment
generated for tests
$end
$timescale {timescale} $end
$scope module top $end
{vars}
$upscope $end
$enddefinitions $end
"""

VAR = "$var wire 1 {ident} {name} $end\n"


def build_vcd(
    body: str,
    timescale: str = "1ns",
    signals: dict[str, str] | None = None,
    header_before_vars: str = "",
) -> str:
    """Build a minimal VCD; signals maps identifier -> name."""
    if signals is None:
        signals = {"!": "clk", '"': "data"}
    vars_text = header_before_vars + "".join(
        VAR.format(ident=i, name=n) for i, n in signals.items()
    )
    return HEADER.format(timescale=timescale, vars=vars_text) + body
