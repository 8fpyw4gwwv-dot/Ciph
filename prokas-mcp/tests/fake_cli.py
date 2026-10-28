"""Stand-in for a headless Berichtswesen launcher, used by the tests.

FAKE_CLI_FAIL_FOR=<Z-codes> and FAKE_CLI_MODE=exit|empty|garbage|timeout|nofile control failures.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fixture_data import write_txt  # noqa: E402

args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
z, out = args["--liste"], Path(args["--out"])
fail_for = os.environ.get("FAKE_CLI_FAIL_FOR", "")
mode = os.environ.get("FAKE_CLI_MODE", "ok") if (not fail_for or z in fail_for.split(",")) else "ok"
calls = os.environ.get("FAKE_CLI_CALLS")
if calls:
    with open(calls, "a") as fh:
        fh.write(z + "\n")

if mode == "exit":
    print("Fehler: Liste nicht gefunden", file=sys.stderr)
    sys.exit(3)
if mode == "timeout":
    time.sleep(30)
if mode == "nofile":
    sys.exit(0)
if mode == "empty":
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"")
    sys.exit(0)
if mode == "garbage":
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("Das ist keine Liste\n1\t2\n", encoding="cp1252")
    sys.exit(0)
write_txt(z, out.parent, out.name, period=(args.get("--von"), args.get("--bis")))
