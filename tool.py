#!/usr/bin/env python3
"""Offline, read-only audit. Python 3.12+, standard library only."""
import argparse
import json
import math
import re
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50000
MAX_FINDINGS = 100

class InputError(ValueError):
    """A safe, non-payload-bearing input error code."""

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError("invalid_arguments")

def read_text(path):
    p = Path(path)
    if not stat.S_ISREG(p.stat().st_mode):
        raise InputError("regular_file_required")
    with p.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError("file_size_limit")
    return data.decode("utf-8")

def reject_constant(value):
    raise InputError("non_finite_json_number")

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate_json_member")
        result[key] = value
    return result

def parse_json(text):
    try:
        result = json.loads(text, object_pairs_hook=unique_pairs,
                            parse_float=Decimal, parse_constant=reject_constant)
    except InputError:
        raise
    except (ValueError, RecursionError, InvalidOperation):
        raise InputError("invalid_json") from None
    stack, nodes = [(result, 0)], 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 128 or nodes > 200000:
            raise InputError("json_structure_limit")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    return result

class Report:
    def __init__(self, tool):
        self.tool, self.findings, self.count = tool, [], 0
    def add(self, code, **location):
        self.count += 1
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(dict(code=code, **location))
    def finish(self, **summary):
        return {"tool": self.tool, "status": "fail" if self.count else "pass",
                "summary": dict(summary, finding_count=self.count),
                "findings": self.findings,
                "findings_truncated": self.count > len(self.findings)}

def emit_main(run):
    try:
        result = run()
        code = 1 if result["status"] == "fail" else 0
    except InputError as exc:
        result, code = {"tool": TOOL, "status": "error", "error": str(exc)}, 2
    except (OSError, UnicodeError):
        result, code = {"tool": TOOL, "status": "error", "error": "unreadable_utf8_input"}, 2
    except (ValueError, RecursionError, InvalidOperation):
        result, code = {"tool": TOOL, "status": "error", "error": "invalid_input"}, 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))
    return code

TOOL = "timestamp-unit-audit"

from datetime import datetime, timezone

UNITS = {"seconds": 1, "milliseconds": 1000, "microseconds": 1000000}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

def boundary(value):
    if not isinstance(value, str):
        raise InputError("invalid_time_window")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InputError("invalid_time_window") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise InputError("timezone_required")
    try:
        delta = stamp.astimezone(timezone.utc) - EPOCH
    except (OverflowError, ValueError):
        raise InputError("invalid_time_window") from None
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds

def audit(values, *, start, end, expected=None):
    low, high = boundary(start), boundary(end)
    if low > high or (expected is not None and expected not in UNITS):
        raise InputError("invalid_time_window_or_unit")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_RECORDS:
        raise InputError("nonempty_integer_array_required")
    report = Report(TOOL)
    candidates = {name: 0 for name in UNITS}
    rows, ambiguous = [], 0
    for number, value in enumerate(values, 1):
        if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,30}", value):
            value = int(value)
        if type(value) is not int or abs(value) >= 10 ** 30:
            raise InputError("integer_timestamp_required")
        possible = [name for name, divisor in UNITS.items()
                    if low <= value * (1000000 // divisor) <= high]
        for name in possible:
            candidates[name] += 1
        if len(rows) < MAX_FINDINGS:
            rows.append({"record": number, "candidate_units": possible})
        if not possible:
            report.add("no_unit_in_window", record=number)
        elif len(possible) > 1:
            ambiguous += 1
            report.add("ambiguous_unit", record=number, candidate_units=possible)
        if expected is not None and expected not in possible:
            report.add("expected_unit_outside_window", record=number, expected_unit=expected)
    result = report.finish(records=len(values), ambiguous_records=ambiguous,
                           candidate_counts=candidates, expected_unit=expected,
                           window_inclusive=True)
    result["records"] = rows
    result["records_truncated"] = len(values) > len(rows)
    return result

def run():
    parser = Parser(description=__doc__)
    parser.add_argument("input", help="JSON array of integer epochs or integer strings")
    parser.add_argument("--start", required=True, help="Inclusive ISO 8601 bound with timezone")
    parser.add_argument("--end", required=True, help="Inclusive ISO 8601 bound with timezone")
    parser.add_argument("--expected", choices=tuple(UNITS))
    args = parser.parse_args()
    return audit(parse_json(read_text(args.input)), start=args.start, end=args.end, expected=args.expected)

if __name__ == "__main__":
    sys.exit(emit_main(run))
