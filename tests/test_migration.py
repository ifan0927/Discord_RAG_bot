import json
import io
import unittest
from unittest import mock
from contextlib import redirect_stdout

from src import cli
from src.migration import (
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    _check_constraints,
)


class MigrationCheckTest(unittest.TestCase):
    def test_constraint_fragments_are_reported(self):
        constraint_defs = {
            table_name: list(fragments)
            for table_name, fragments in REQUIRED_CONSTRAINTS.items()
        }

        result = _check_constraints(constraint_defs)

        self.assertTrue(all(all(items.values()) for items in result.values()))

    def test_constraint_fragments_report_missing_items(self):
        result = _check_constraints({"raw_messages": []})

        self.assertFalse(result["raw_messages"]["CHECK ((source = ANY"])

    def test_cli_check_exits_nonzero_when_schema_is_missing(self):
        fake_result = mock.Mock()
        fake_result.ok = False
        fake_result.to_json.return_value = json.dumps({"ok": False})

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.check_schema", return_value=fake_result) as check_schema:
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["migrate", "check"])

        self.assertEqual(exit_code, 1)
        check_schema.assert_called_once_with("postgresql://local/test")

    def test_cli_up_uses_database_url(self):
        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.apply_schema") as apply_schema:
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["migrate", "up"])

        self.assertEqual(exit_code, 0)
        apply_schema.assert_called_once_with("postgresql://local/test")

    def test_expected_schema_inventory_matches_issue_scope(self):
        self.assertEqual(
            REQUIRED_TABLES,
            ["raw_messages", "conversation_chunks", "hourly_summaries", "sessions"],
        )
        self.assertIn("conversation_chunks_embedding_hnsw_idx", REQUIRED_INDEXES)
        self.assertIn("sessions_channel_thread_expires_at_idx", REQUIRED_INDEXES)


if __name__ == "__main__":
    unittest.main()
