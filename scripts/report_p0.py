"""Publish only synthetic benchmark numbers and gate totals to the CI summary."""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path("data/p0-reports")
lines = ["## Multi-user P0 prototypes"]
if (root / "junit.xml").exists():
    suites = ET.parse(root / "junit.xml").getroot()
    totals = {
        k: sum(int(s.get(k, "0")) for s in suites.findall("testsuite"))
        for k in ("tests", "failures", "errors", "skipped")
    }
    lines.append(json.dumps(totals))
if (root / "admission-baseline.json").exists():
    lines.append("Synthetic durable-run admission only; not Agent/model latency or a P5 pass.")
    lines.append("```json\n" + (root / "admission-baseline.json").read_text() + "\n```")
else:
    lines.append("No real-service admission report was produced.")
text = "\n".join(lines)
print(text)
if target := os.getenv("GITHUB_STEP_SUMMARY"):
    with Path(target).open("a", encoding="utf-8") as output:
        output.write(text + "\n")
