"""HTTP layer for the VCD windowing service."""

from __future__ import annotations

import json
import os
from typing import Optional

from flask import Flask, jsonify, request

from .vcd import (
    MAX_FILE_BYTES,
    SOURCE_CANDIDATE,
    SOURCE_REFERENCE,
    VCDError,
    bad_request,
    compare,
    parse_pairs,
    parse_signals,
    parse_window,
    process,
)


def create_app() -> Flask:
    app = Flask(__name__)
    # Each uploaded VCD is capped precisely at 4 MiB on the decoded part
    # below. /api/vcd/compare accepts two files, so allow two 4 MiB parts
    # plus headroom for the multipart envelope.
    app.config["MAX_CONTENT_LENGTH"] = 2 * MAX_FILE_BYTES + 2 * 1024 * 1024

    @app.errorhandler(413)
    def too_large(_err: Exception) -> tuple:
        if request.path == "/api/vcd/compare":
            # Attribute envelope-level rejections for compare.
            return jsonify(
                {
                    "error": {
                        "code": "FILE_TOO_LARGE",
                        "message": "upload exceeds the 4 MiB per-file limit",
                        "source": "comparison",
                    }
                }
            ), 413
        return (
            jsonify(
                {
                    "error": {
                        "code": "FILE_TOO_LARGE",
                        "message": "uploaded VCD exceeds the 4 MiB limit",
                    }
                }
            ),
            413,
        )

    @app.get("/healthz")
    def healthz() -> tuple:
        return jsonify({"status": "ok"}), 200

    @app.post("/api/vcd/window")
    def vcd_window() -> tuple:
        try:
            return _handle_window()
        except VCDError as exc:
            body, status = exc.to_response()
            return jsonify(body), status

    @app.post("/api/vcd/compare")
    def vcd_compare() -> tuple:
        try:
            return _handle_compare()
        except VCDError as exc:
            body, status = exc.to_response()
            # The compare contract attributes every failure to one side.
            body["error"]["source"] = exc.source
            return jsonify(body), status

    def _read_upload(field: str, source: str) -> str:
        if field not in request.files:
            raise bad_request(
                "MISSING_FIELD",
                f"multipart field {field!r} is required",
                source=source,
            )
        upload = request.files[field]
        raw = upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise VCDError(
                "FILE_TOO_LARGE",
                f"uploaded VCD exceeds {MAX_FILE_BYTES} bytes (4 MiB)",
                http_status=413,
                source=source,
            )
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise VCDError(
                "NOT_ASCII",
                "the VCD file must be ASCII encoded",
                details={"byte_offset": exc.start},
                http_status=400,
                source=source,
            ) from exc

    def _handle_window() -> tuple:
        # Note: request.content_length includes the multipart envelope, so
        # the 4 MiB rule is enforced on the decoded file part below instead.
        if "file" not in request.files:
            raise bad_request("MISSING_FIELD", "multipart field 'file' is required")
        upload = request.files["file"]
        raw = upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise VCDError(
                "FILE_TOO_LARGE",
                f"uploaded VCD exceeds {MAX_FILE_BYTES} bytes (4 MiB)",
                http_status=413,
            )
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise bad_request(
                "NOT_ASCII",
                "the VCD file must be ASCII encoded",
                details={"byte_offset": exc.start},
            ) from exc

        window = parse_window(_form_value("window"))
        signals = parse_signals(request.form.getlist("signals"))

        result = process(text, signals, window)
        return jsonify(result), 200

    def _handle_compare() -> tuple:
        # Request-level problems (window, pairs, limits) are attributed to
        # "comparison"; per-file parse problems are re-sourced below.
        window = parse_window(_form_value("window"))
        pairs = parse_pairs(request.form.getlist("pair"))
        reference_text = _read_upload("reference", SOURCE_REFERENCE)
        candidate_text = _read_upload("candidate", SOURCE_CANDIDATE)

        result = compare(reference_text, candidate_text, pairs, window)
        return jsonify(result), 200

    def _form_value(key: str) -> Optional[str]:
        values = request.form.getlist(key)
        if len(values) > 1:
            raise bad_request("DUPLICATE_FIELD", f"form field {key!r} appears more than once")
        return values[0] if values else None

    @app.errorhandler(405)
    def method_not_allowed(_err: Exception) -> tuple:
        return jsonify({"error": {"code": "METHOD_NOT_ALLOWED", "message": "method not allowed"}}), 405

    @app.errorhandler(404)
    def not_found(_err: Exception) -> tuple:
        return jsonify({"error": {"code": "NOT_FOUND", "message": "not found"}}), 404

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
