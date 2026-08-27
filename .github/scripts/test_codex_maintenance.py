#!/usr/bin/env python3
"""Unit tests for the repository-native maintenance state machine."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("codex_maintenance.py")
SPEC = importlib.util.spec_from_file_location("codex_maintenance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MAINTENANCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAINTENANCE)


class MaintenanceStateTests(unittest.TestCase):
    repository = "example/codex"

    def summary(self, *, active=None, failed=None, new=None, cancelled=None):
        return {
            "main_head_sha": "main-sha",
            "open_pull_requests": 3,
            "open_draft_pull_requests": [65, 66],
            "active_workflow_runs": active or [],
            "failed_workflow_runs": failed or [],
            "new_failure_runs": new or [],
            "cancelled_workflow_runs": cancelled or [],
            "workflow_status_counts": {},
            "workflow_conclusion_counts": {},
            "workflow_window_limited_to": 100,
        }

    def test_advances_cycle_and_records_bounded_observation(self):
        state = MAINTENANCE.new_state(self.repository)
        summary = self.summary(active=[{"id": 1001}])

        updated, should_write, record = MAINTENANCE.advance_state(
            state,
            summary,
            run_id="run-1",
            now="2026-08-26T00:00:00Z",
        )

        self.assertTrue(should_write)
        self.assertEqual(updated["completed_cycles"], 1)
        self.assertEqual(updated["last_result"], "monitoring")
        self.assertEqual(updated["latest_cycle_path"], ".github/maintenance/cycles/0001.json")
        self.assertEqual(updated["recent_history"][0]["cycle"], 1)
        self.assertEqual(record["active_workflow_run_ids"], [1001])
        MAINTENANCE.validate_state(updated, self.repository)

    def test_deduplicates_known_failures_but_preserves_new_failure(self):
        snapshot = {
            "pull_requests": [],
            "workflow_window_limited_to": 100,
            "workflow_runs": [
                {"id": 11, "status": "completed", "conclusion": "failure"},
                {"id": 12, "status": "completed", "conclusion": "timed_out"},
                {"id": 13, "status": "queued", "conclusion": None},
            ],
            "head_sha": "main-sha",
        }

        summary = MAINTENANCE.summarize_snapshot(snapshot, {"11": {}})

        self.assertEqual([run["id"] for run in summary["new_failure_runs"]], [12])
        self.assertEqual([run["id"] for run in summary["active_workflow_runs"]], [13])
        result, next_action = MAINTENANCE.determine_next_action(summary)
        self.assertEqual(result, "review_required")
        self.assertIn("supervised repair task", next_action)

    def test_strips_terminal_color_before_json_parsing(self):
        colored = "\x1b[1;37m[\x1b[m"
        self.assertEqual(MAINTENANCE.ANSI_ESCAPE.sub("", colored), "[")

    def test_migrates_legacy_history_to_bounded_recent_index(self):
        legacy = {
            "schema_version": 1,
            "repository": self.repository,
            "max_cycles": MAINTENANCE.MAX_CYCLES,
            "completed_cycles": 1,
            "created_at": "2026-08-26T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
            "last_result": "review_required",
            "next_recommended_action": "review",
            "known_failure_runs": {},
            "history": [{"cycle": 1}],
        }

        migrated = MAINTENANCE.normalize_state(legacy)

        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["legacy_history_entries"], 1)
        self.assertEqual(migrated["latest_cycle_path"], ".github/maintenance/cycles/0001.json")
        self.assertEqual(migrated["recent_history"], [{"cycle": 1}])
        MAINTENANCE.validate_state(migrated, self.repository)

    def test_hard_cycle_limit_does_not_write_another_record(self):
        state = MAINTENANCE.new_state(self.repository)
        state["completed_cycles"] = MAINTENANCE.MAX_CYCLES
        state["latest_cycle_path"] = ".github/maintenance/cycles/2400.json"
        summary = self.summary()

        updated, should_write, record = MAINTENANCE.advance_state(
            state,
            summary,
            run_id="run-limit",
            now="2026-08-26T00:00:00Z",
        )

        self.assertFalse(should_write)
        self.assertIsNone(record)
        self.assertEqual(updated["completed_cycles"], MAINTENANCE.MAX_CYCLES)
        self.assertEqual(updated["last_result"], "cycle_limit_reached")
        self.assertEqual(updated["recent_history"], [])


if __name__ == "__main__":
    unittest.main()
