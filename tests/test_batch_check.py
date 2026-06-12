from __future__ import annotations

from datetime import date, datetime, timezone
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from src import cli
from src.batch_check import check_batch_day


class FakeSchema:
    def __init__(self, ok=True):
        self.ok = ok


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, raw_rows=None, chunk_rows=None, summary_rows=None):
        self.raw_rows = raw_rows or []
        self.chunk_rows = chunk_rows or []
        self.summary_rows = summary_rows or []
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if "FROM raw_messages" in sql:
            return FakeResult(self.raw_rows)
        if "FROM conversation_chunks" in sql:
            return FakeResult(self.chunk_rows)
        if "FROM hourly_summaries" in sql:
            return FakeResult(self.summary_rows)
        return FakeResult([])


def raw_row(message_id: int, hour: int = 9, minute: int = 0):
    return (
        message_id,
        datetime(2026, 6, 10, hour - 8, minute, tzinfo=timezone.utc),
        9000 + message_id,
        f"message {message_id}",
    )


def env(root: str):
    return {
        "CHUNK_STRATEGY_VERSION": "timegap-v1",
        "SUMMARY_STRATEGY_VERSION": "hourly-v1",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "CHUNK_WINDOW_MESSAGES": "20",
        "CHUNK_STRIDE_MESSAGES": "10",
        "MIN_CHUNK_MESSAGES": "5",
        "CHUNK_TEXT_MAX_CHARS": "8000",
        "MIN_SUMMARY_LLM_MESSAGES": "5",
        "SUMMARY_INPUT_TOKEN_BUDGET": "6000",
        "SUMMARY_MAX_OUTPUT_TOKENS": "300",
        "SUMMARY_FALLBACK_MAX_CHARS": "4000",
        "SUMMARY_LLM_MAX_RETRIES": "2",
        "BATCH_STAGING_DIR": root,
    }


def chunk_env(root: str):
    values = env(root)
    for key in [
        "SUMMARY_STRATEGY_VERSION",
        "MIN_SUMMARY_LLM_MESSAGES",
        "SUMMARY_INPUT_TOKEN_BUDGET",
        "SUMMARY_MAX_OUTPUT_TOKENS",
        "SUMMARY_FALLBACK_MAX_CHARS",
        "SUMMARY_LLM_MAX_RETRIES",
    ]:
        values.pop(key)
    return values


def summary_env(root: str):
    values = env(root)
    for key in [
        "CHUNK_STRATEGY_VERSION",
        "CHUNK_WINDOW_MESSAGES",
        "CHUNK_STRIDE_MESSAGES",
        "MIN_CHUNK_MESSAGES",
        "CHUNK_TEXT_MAX_CHARS",
    ]:
        values.pop(key)
    return values


class BatchCheckTest(unittest.TestCase):
    def test_completed_day_passes_when_db_artifacts_match_expected_keys(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(
            raw_rows=rows,
            chunk_rows=[("chunk:timegap-v1:2026-06-10:1:5", 1536)],
            summary_rows=[("summary:hourly-v1:2026-06-10T09:00:00+08:00:1:5", 1536)],
        )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", env(tmp), clear=False):
                result = check_batch_day(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.raw_eligible_count, 5)
        self.assertEqual(result.expected_artifact_counts, {"chunks": 1, "summaries": 1})
        self.assertEqual(result.actual_artifact_counts, {"chunks": 1, "summaries": 1})
        self.assertEqual(result.missing_keys, {"chunks": [], "summaries": []})
        self.assertTrue(result.embedding_dimension_status["chunks"]["ok"])

    def test_stale_day_reports_missing_and_unexpected_keys_after_raw_changes(self):
        rows = [raw_row(index, 9, index) for index in range(1, 7)]
        fake_conn = FakeConnection(
            raw_rows=rows,
            chunk_rows=[("chunk:timegap-v1:2026-06-10:1:5", 1536)],
            summary_rows=[("summary:hourly-v1:2026-06-10T09:00:00+08:00:1:5", 1536)],
        )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", env(tmp), clear=False):
                result = check_batch_day(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.missing_keys["chunks"],
            ["chunk:timegap-v1:2026-06-10:1:6"],
        )
        self.assertEqual(
            result.unexpected_keys["chunks"],
            ["chunk:timegap-v1:2026-06-10:1:5"],
        )
        self.assertEqual(
            result.missing_keys["summaries"],
            ["summary:hourly-v1:2026-06-10T09:00:00+08:00:1:6"],
        )

    def test_zero_eligible_day_passes_only_when_artifacts_are_empty(self):
        fake_conn = FakeConnection()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", env(tmp), clear=False):
                result = check_batch_day(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.raw_eligible_count, 0)
        self.assertEqual(result.expected_artifact_counts, {"chunks": 0, "summaries": 0})

    def test_chunk_only_check_does_not_require_summary_settings(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(
            raw_rows=rows,
            chunk_rows=[("chunk:timegap-v1:2026-06-10:1:5", 1536)],
        )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", chunk_env(tmp), clear=True):
                result = check_batch_day(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    only="chunks",
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.chunk_strategy_version, "timegap-v1")
        self.assertIsNone(result.summary_strategy_version)
        self.assertEqual(result.expected_artifact_counts, {"chunks": 1, "summaries": 0})

    def test_summary_only_check_does_not_require_chunk_settings(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(
            raw_rows=rows,
            summary_rows=[("summary:hourly-v1:2026-06-10T09:00:00+08:00:1:5", 1536)],
        )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", summary_env(tmp), clear=True):
                result = check_batch_day(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    only="summaries",
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        self.assertTrue(result.ok)
        self.assertIsNone(result.chunk_strategy_version)
        self.assertEqual(result.summary_strategy_version, "hourly-v1")
        self.assertEqual(result.expected_artifact_counts, {"chunks": 0, "summaries": 1})

    def test_check_batch_cli_emits_json_only_and_uses_exit_code(self):
        fake_result = mock.Mock()
        fake_result.ok = False
        fake_result.to_json.return_value = json.dumps({"ok": False, "missing_keys": {"chunks": ["k"]}})

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.check_batch_day", return_value=fake_result):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = cli.main(["check-batch", "--date", "2026-06-10", "--json"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": False, "missing_keys": {"chunks": ["k"]}})
        self.assertEqual(stderr.getvalue(), "")

    def test_check_batch_cli_config_errors_are_json_only_failures(self):
        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.check_batch_day", side_effect=RuntimeError("missing setting")):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = cli.main(["check-batch", "--date", "2026-06-10", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["day"], "2026-06-10")
        self.assertEqual(payload["error"], "missing setting")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertEqual(stderr.getvalue(), "")

    def test_backfill_resume_skips_matching_days_and_reruns_stale_days(self):
        checks = [
            mock.Mock(ok=True, to_json=lambda: json.dumps({"ok": True})),
            mock.Mock(ok=False, to_json=lambda: json.dumps({"ok": False})),
        ]
        rerun_payload = {"day": "2026-06-11", "status": "success"}

        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://local/test", "BATCH_EMBEDDING_PROVIDER": "fake"},
        ):
            with mock.patch("src.cli.check_batch_day", side_effect=checks):
                with mock.patch("src.cli._run_real_batch_day", return_value=rerun_payload) as run_day:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        exit_code = cli.main(
                            [
                                "backfill",
                                "--start",
                                "2026-06-10",
                                "--end",
                                "2026-06-11",
                                "--only",
                                "all",
                                "--resume",
                            ]
                        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["skipped_days"], ["2026-06-10"])
        self.assertEqual(payload["failed_days"], [])
        run_day.assert_called_once()
        self.assertEqual(run_day.call_args.kwargs["day"], date(2026, 6, 11))

    def test_backfill_continues_after_failed_day_and_returns_nonzero(self):
        payloads = [
            {"day": "2026-06-10", "status": "failed"},
            {"day": "2026-06-11", "status": "success"},
        ]

        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://local/test", "BATCH_EMBEDDING_PROVIDER": "fake"},
        ):
            with mock.patch("src.cli._run_real_batch_day", side_effect=payloads) as run_day:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = cli.main(
                        [
                            "backfill",
                            "--start",
                            "2026-06-10",
                            "--end",
                            "2026-06-11",
                            "--only",
                            "chunks",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failed_days"], ["2026-06-10"])
        self.assertEqual(run_day.call_count, 2)

    def test_backfill_continues_after_partial_failed_day_and_returns_nonzero(self):
        payloads = [
            {"day": "2026-06-10", "status": "partial_failed"},
            {"day": "2026-06-11", "status": "success"},
        ]

        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://local/test", "BATCH_EMBEDDING_PROVIDER": "fake"},
        ):
            with mock.patch("src.cli._run_real_batch_day", side_effect=payloads) as run_day:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = cli.main(
                        [
                            "backfill",
                            "--start",
                            "2026-06-10",
                            "--end",
                            "2026-06-11",
                            "--only",
                            "all",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failed_days"], ["2026-06-10"])
        self.assertEqual(run_day.call_count, 2)


if __name__ == "__main__":
    unittest.main()
