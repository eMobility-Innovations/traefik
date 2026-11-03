#!/usr/bin/env python3
"""Enforce coverage of every owned production Python module in the overlay."""

import dis
import json
import trace
import unittest
from pathlib import Path
from types import CodeType

ROOT = Path(__file__).resolve().parents[2]


def executable_lines(code):
    lines = {line for _, line in dis.findlinestarts(code) if line and line > 0}
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            lines.update(executable_lines(constant))
    return lines


def coverage_report(root, counts):
    files = {}
    for path in sorted((root / "overlay").rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[1] == "tests" or relative.as_posix() == "overlay/verify_image_startup.py":
            continue
        executable = executable_lines(compile(path.read_text(), str(path), "exec"))
        hit = {line for (filename, line), count in counts.items() if filename == str(path) and count}
        files[relative.as_posix()] = {"covered_lines": len(executable & hit),
                                      "executable_lines": len(executable),
                                      "missing_lines": sorted(executable - hit)}
    covered = sum(item["covered_lines"] for item in files.values())
    total = sum(item["executable_lines"] for item in files.values())
    return {"covered_lines": covered, "executable_lines": total,
            "percent": round(100 * covered / total, 2) if total else 0, "files": files}


def passes(report):
    return report["executable_lines"] > 0 and report["covered_lines"] * 100 >= report["executable_lines"] * 80


def run_suite():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "overlay/tests"), pattern="test_*.py")
    return unittest.TextTestRunner().run(suite)


def main():
    tracer = trace.Trace(count=True, trace=False)
    result = tracer.runfunc(run_suite)
    report = coverage_report(ROOT, tracer.results().counts)
    print(json.dumps({"owned_python_coverage": report}, sort_keys=True))
    return 0 if result.wasSuccessful() and passes(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
