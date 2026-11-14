#!/usr/bin/env python3
"""Python 3.11-compatible per-test timing without altering unittest outcomes."""
from datetime import datetime, timezone
import json
import time
import unittest


class TimedTestResult(unittest.TextTestResult):
    def _record(self, event, test, **fields):
        # Test IDs only: never serialize subtest parameters, locals or fixtures.
        record = {"event": event, "test": test.id(),
                  "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
        self.stream.writeln()
        self.stream.writeln("VALIDATION_TEST " + json.dumps(record, sort_keys=True))
        self.stream.flush()

    def startTest(self, test):
        super().startTest(test)
        self._started = time.monotonic()
        self._record("begin", test)

    def stopTest(self, test):
        elapsed = time.monotonic() - self._started
        try:
            self._record("end", test, elapsed_seconds=round(elapsed, 6))
        finally:
            super().stopTest(test)


class TimedTestRunner(unittest.TextTestRunner):
    resultclass = TimedTestResult
