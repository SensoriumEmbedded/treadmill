"""Run records and per-test artifacts: ``reports/<timestamp>/run.json``,
``report.html`` rendered from it and, per test, the serial log slice, the
events and any frames."""

from treadmill.report.artifacts import (  # noqa: F401
    RunDir, describe_board, describe_station, host_record, safe_name, suite_commit, write_board_artifacts,
)
from treadmill.report.html import render_report  # noqa: F401
