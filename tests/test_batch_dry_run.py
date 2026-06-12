from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from src import cli
from src.batch_dry_run import (
    BatchDryRunSettings,
    parse_day,
    run_batch_dry_run,
)


class FakeSchema:
    def __init__(self, ok=True):
        self.ok = ok


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, day_rows=None, hour_rows=None):
        self.day_rows = day_rows or []
        self.hour_rows = hour_rows or []
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if "date_trunc('hour'" in sql:
            return FakeResult(self.hour_rows)
        return FakeResult(self.day_rows)


def settings(**overrides):
    values = {
        "chunk_strategy_version": "timegap-v1",
        "summary_strategy_version": "hourly-v1",
        "chunk_window_messages": 20,
        "chunk_stride_messages": 10,
        "min_chunk_messages": 5,
        "min_summary_llm_messages": 5,
        "summary_input_token_budget": 6000,
        "summary_max_output_tokens": 300,
        "embedding_price_per_1m_tokens": Decimal("0.02"),
        "summary_input_price_per_1m_tokens": Decimal("0.10"),
        "summary_output_price_per_1m_tokens": Decimal("0.40"),
    }
    values.update(overrides)
    return BatchDryRunSettings(**values)


class BatchDryRunTest(unittest.TestCase):
    def test_single_day_estimates_counts_tokens_and_cost_without_artifact_sql(self):
        fake_conn = FakeConnection(
            day_rows=[(date(2026, 6, 10), 25)],
            hour_rows=[
                (date(2026, 6, 10), datetime(2026, 6, 10, 9, 0), 4),
                (date(2026, 6, 10), datetime(2026, 6, 10, 10, 0), 21),
            ],
        )

        result = run_batch_dry_run(
            database_url="postgresql://local/test",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            only="all",
            command="run-batch",
            settings=settings(),
            schema_checker=lambda database_url: FakeSchema(ok=True),
            connect=lambda database_url: fake_conn,
        )

        self.assertEqual(result.totals["eligible_messages"], 25)
        self.assertEqual(result.totals["expected_chunks"], 3)
        self.assertEqual(result.totals["expected_summaries"], 2)
        self.assertEqual(result.days[0].token_estimate["30"]["chunk_embedding_tokens"], 1200)
        self.assertEqual(result.days[0].token_estimate["30"]["summary_llm_input_tokens"], 630)
        self.assertEqual(result.days[0].token_estimate["30"]["summary_llm_output_tokens"], 300)
        self.assertEqual(result.warnings, [])
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertIn("raw_messages", executed_sql)
        self.assertNotIn("conversation_chunks", executed_sql)
        self.assertNotIn("hourly_summaries", executed_sql)
        self.assertNotIn("INSERT", executed_sql.upper())
        self.assertNotIn("DELETE", executed_sql.upper())
        self.assertEqual(
            fake_conn.executions[0][1]["start_at"],
            datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            fake_conn.executions[0][1]["end_at"],
            datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc),
        )

    def test_zero_eligible_day_returns_success_empty_shape(self):
        result = run_batch_dry_run(
            database_url="postgresql://local/test",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            only="all",
            command="run-batch",
            settings=settings(),
            schema_checker=lambda database_url: FakeSchema(ok=True),
            connect=lambda database_url: FakeConnection(),
        )

        self.assertEqual(result.days[0].eligible_messages, 0)
        self.assertEqual(result.days[0].expected_chunks, 0)
        self.assertEqual(result.days[0].expected_summaries, 0)
        self.assertEqual(result.totals["eligible_messages"], 0)

    def test_range_includes_empty_days_and_accumulates_totals(self):
        fake_conn = FakeConnection(
            day_rows=[("2026-06-10", 6), ("2026-06-12", 5)],
            hour_rows=[
                ("2026-06-10", datetime(2026, 6, 10, 1, 0), 6),
                ("2026-06-12", datetime(2026, 6, 12, 1, 0), 5),
            ],
        )

        result = run_batch_dry_run(
            database_url="postgresql://local/test",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 12),
            only="summaries",
            command="backfill",
            settings=settings(),
            schema_checker=lambda database_url: FakeSchema(ok=True),
            connect=lambda database_url: fake_conn,
        )

        self.assertEqual([day.day for day in result.days], ["2026-06-10", "2026-06-11", "2026-06-12"])
        self.assertEqual(result.totals["eligible_messages"], 11)
        self.assertEqual(result.totals["expected_chunks"], 0)
        self.assertEqual(result.totals["expected_summaries"], 2)

    def test_missing_unit_prices_warns_and_marks_cost_unknown(self):
        result = run_batch_dry_run(
            database_url="postgresql://local/test",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            only="chunks",
            command="run-batch",
            settings=settings(embedding_price_per_1m_tokens=None),
            schema_checker=lambda database_url: FakeSchema(ok=True),
            connect=lambda database_url: FakeConnection(day_rows=[(date(2026, 6, 10), 10)]),
        )

        self.assertEqual(result.days[0].estimated_cost_usd, "unknown")
        self.assertEqual(result.totals["estimated_cost_usd"], "unknown")
        self.assertIn("EMBEDDING_PRICE_PER_1M_TOKENS", result.warnings[0])

    def test_chunk_only_cost_does_not_require_summary_prices(self):
        result = run_batch_dry_run(
            database_url="postgresql://local/test",
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            only="chunks",
            command="run-batch",
            settings=settings(
                summary_input_price_per_1m_tokens=None,
                summary_output_price_per_1m_tokens=None,
            ),
            schema_checker=lambda database_url: FakeSchema(ok=True),
            connect=lambda database_url: FakeConnection(day_rows=[(date(2026, 6, 10), 10)]),
        )

        self.assertIsInstance(result.days[0].estimated_cost_usd, dict)
        self.assertEqual(result.warnings, [])

    def test_invalid_schema_or_dates_fail_before_querying(self):
        fake_conn = FakeConnection()
        with self.assertRaisesRegex(RuntimeError, "schema check failed"):
            run_batch_dry_run(
                database_url="postgresql://local/test",
                start_date=date(2026, 6, 10),
                end_date=date(2026, 6, 10),
                only="all",
                command="run-batch",
                settings=settings(),
                schema_checker=lambda database_url: FakeSchema(ok=False),
                connect=lambda database_url: fake_conn,
            )
        self.assertEqual(fake_conn.executions, [])

        with self.assertRaisesRegex(RuntimeError, "end date"):
            run_batch_dry_run(
                database_url="postgresql://local/test",
                start_date=date(2026, 6, 12),
                end_date=date(2026, 6, 10),
                only="all",
                command="backfill",
                settings=settings(),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        with self.assertRaisesRegex(RuntimeError, "Invalid date"):
            parse_day("2026-99-10")

    def test_cli_run_batch_dry_run_prints_json(self):
        fake_result = mock.Mock()
        fake_result.to_json.return_value = json.dumps({"dry_run": True})

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.run_batch_dry_run", return_value=fake_result) as dry_run:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"dry_run": True})
        dry_run.assert_called_once()
        self.assertEqual(dry_run.call_args.kwargs["only"], "all")

    def test_cli_backfill_dry_run_prints_json(self):
        fake_result = mock.Mock()
        fake_result.to_json.return_value = json.dumps({"command": "backfill", "dry_run": True})

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli.run_batch_dry_run", return_value=fake_result) as dry_run:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli.main(
                        [
                            "backfill",
                            "--start",
                            "2026-06-10",
                            "--end",
                            "2026-06-12",
                            "--only",
                            "summaries",
                            "--dry-run",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"command": "backfill", "dry_run": True})
        dry_run.assert_called_once()
        self.assertEqual(dry_run.call_args.kwargs["only"], "summaries")

    def test_cli_backfill_non_dry_run_uses_real_backfill(self):
        fake_payload = {
            "command": "backfill",
            "status": "success",
            "failed_days": [],
        }

        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://local/test", "BATCH_EMBEDDING_PROVIDER": "fake"},
        ):
            with mock.patch("src.cli._run_backfill", return_value=fake_payload) as backfill:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli.main(
                        ["backfill", "--start", "2026-06-10", "--end", "2026-06-11", "--only", "all"]
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), fake_payload)
        backfill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
