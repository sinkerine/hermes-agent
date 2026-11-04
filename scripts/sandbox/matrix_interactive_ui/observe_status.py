"""Read-only store timing receipt: run immediately before submitting in Element."""

import argparse
from pathlib import Path
import sqlite3
import time

from common import HOME, SB, entrypoint, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="Original UI event ID")
    args = parser.parse_args()
    path = HOME / "platforms/matrix/store/interactive_ui.sqlite3"
    assert path.is_file() and path.resolve().is_relative_to(HOME)
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        def read():
            return connection.execute("SELECT revision,status FROM uis WHERE ui_event_id=?", (args.event,)).fetchone()
        first = read()
        assert first and first[1] == "active", "Observe an active card before clicking"
        started = time.monotonic()
        print("Watching active card for eight seconds; submit in Element now.", flush=True)
        observed = first
        while time.monotonic() - started < 8:
            observed = read()
            if observed and observed[1] == "submitted":
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        write_json(SB / f"evidence/status-{time.time_ns()}.json", {
            "event_id": args.event, "observed_at": time.time(), "elapsed_from_observer_start_s": elapsed,
            "initial_revision": first[0], "observed_revision": observed[0] if observed else None,
            "store_submitted_within_8s": bool(observed and observed[1] == "submitted" and elapsed <= 8),
            "limitation": "Store commit only. Browser screenshot/video and timeline timestamps must prove visible edit before model reply.",
        })
    finally:
        connection.close()


if __name__ == "__main__":
    entrypoint(main)
