"""Load the generated fixture as data, then run real-service probes on Linux."""

import os
import subprocess
import sys
from pathlib import Path

if os.name != "posix":
    raise SystemExit("Real RQ process-recovery probes require Linux; use the P0 CI workflow")
source = Path("data/p0-fixture/.env")
environment = dict(os.environ)
for line in source.read_text(encoding="utf-8").splitlines():
    key, value = line.split("=", 1)
    if not key.startswith("P0_"):
        raise SystemExit("Unexpected generated fixture key")
    environment[key] = value
environment["WMS_P0_LIVE"] = "1"
subprocess.run([sys.executable, "scripts/wait_p0_services.py"], env=environment, check=True)
subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/unit/test_p0_identity.py",
        "tests/integration/test_multiuser_p0_live.py",
        "-q",
        "--junitxml=data/p0-reports/junit.xml",
    ],
    env=environment,
    check=True,
)
subprocess.run([sys.executable, "scripts/report_p0.py"], env=environment, check=True)
