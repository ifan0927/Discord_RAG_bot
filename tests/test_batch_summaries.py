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
from src.batch_summaries import SummaryBatchSettings, run_summaries_batch


class FakeSchema:
    def __init__(self, ok=True):
        self.ok = ok


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, raw_rows=None, existing_batch_rows=None):
        self.raw_rows = raw_rows or []
        self.existing_batch_rows = existing_batch_rows or []
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if "FROM raw_messages" in sql:
            return FakeResult(self.raw_rows)
        if "SELECT batch_id" in sql:
            return FakeResult(self.existing_batch_rows)
        return FakeResult([])


class FakeEmbeddingProvider:
    def __init__(self, dimensions=1536):
        self.dimensions = dimensions
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [[float(index)] * self.dimensions for index, _text in enumerate(texts, 1)]


class FakeSummaryProvider:
    def __init__(self, fail=False, text=None):
        self.fail = fail
        self.text = text
        self.calls = []

    def summarize(self, text, *, max_output_tokens):
        self.calls.append((text, max_output_tokens))
        if self.fail:
            raise RuntimeError("summary provider failed")
        if self.text is not None:
            return self.text
        return "\n".join(
            [
                "時間範圍: 2026-06-10 09:00-10:00 Asia/Taipei",
                "參與者: author:9001, author:9002",
                "重點:",
                "- author:9001 和 author:9002 討論可回查的事項。",
                "待回查線索:",
                "- author:9001 message 1",
            ]
        )


def settings(root: Path, **overrides):
    values = {
        "summary_strategy_version": "hourly-v1",
        "embedding_model": "text-embedding-3-small",
        "min_summary_llm_messages": 5,
        "summary_input_token_budget": 6000,
        "summary_max_output_tokens": 300,
        "summary_fallback_max_chars": 4000,
        "summary_llm_max_retries": 2,
        "batch_staging_dir": root,
    }
    values.update(overrides)
    return SummaryBatchSettings(**values)


def raw_row(message_id: int, hour: int = 9, minute: int = 0, content: str | None = None):
    return (
        message_id,
        datetime(2026, 6, 10, hour - 8, minute, tzinfo=timezone.utc),
        9000 + message_id,
        content or f"message {message_id}",
    )


def raw_row_at(message_id: int, created_at: datetime, content: str | None = None):
    return (
        message_id,
        created_at,
        9000 + message_id,
        content or f"message {message_id}",
    )


class BatchSummariesTest(unittest.TestCase):
    def test_no_eligible_messages_create_no_summary_row(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success_empty")
        self.assertEqual(result.staged_summaries, 0)
        self.assertEqual(result.committed_summaries, 0)
        self.assertEqual(manifest["step_status"], {"summaries": "success_empty"})
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertIn("DELETE FROM hourly_summaries", executed_sql)
        self.assertNotIn("INSERT INTO hourly_summaries", executed_sql)

    def test_below_threshold_uses_raw_lines_without_summary_llm(self):
        rows = [raw_row(index, 9, index) for index in range(1, 4)]
        summary_provider = FakeSummaryProvider()
        embedding_provider = FakeEmbeddingProvider()
        fake_conn = FakeConnection(raw_rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=embedding_provider,
                summary_provider=summary_provider,
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            line = (Path(result.staging_dir) / "summaries.jsonl").read_text(encoding="utf-8").splitlines()[0]
            staged = json.loads(line)
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success")
        self.assertEqual(summary_provider.calls, [])
        self.assertIn("時間範圍: 2026-06-10 09:00-10:00 Asia/Taipei", staged["summary_text"])
        self.assertIn("參與者: author:9001, author:9002, author:9003", staged["summary_text"])
        self.assertIn("重點:", staged["summary_text"])
        self.assertIn("待回查線索:", staged["summary_text"])
        self.assertIn("author:9001 message 1", staged["summary_text"])
        self.assertEqual(manifest["fallback_counts"]["summaries"], 1)
        self.assertEqual(manifest["summary_llm_error_count"], 0)
        self.assertEqual(len(embedding_provider.calls[0]), 1)

    def test_summary_provider_failure_falls_back_to_raw_lines(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(raw_rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                summary_provider=FakeSummaryProvider(fail=True),
                settings=settings(Path(tmp), summary_llm_max_retries=1),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success")
        self.assertEqual(manifest["fallback_counts"]["summaries"], 1)
        self.assertEqual(manifest["summary_llm_error_count"], 2)

    def test_summary_provider_success_uses_generated_summary(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(raw_rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                summary_provider=FakeSummaryProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            line = (Path(result.staging_dir) / "summaries.jsonl").read_text(encoding="utf-8").splitlines()[0]
            staged = json.loads(line)
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success")
        self.assertIn("重點:", staged["summary_text"])
        self.assertIn("author:9001", staged["summary_text"])
        self.assertEqual(manifest["fallback_counts"]["summaries"], 0)

    def test_unstructured_summary_provider_output_falls_back_to_structured_raw_lines(self):
        rows = [raw_row(index, 9, index) for index in range(1, 6)]
        fake_conn = FakeConnection(raw_rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                summary_provider=FakeSummaryProvider(text="generic generated summary"),
                settings=settings(Path(tmp), summary_llm_max_retries=0),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            line = (Path(result.staging_dir) / "summaries.jsonl").read_text(encoding="utf-8").splitlines()[0]
            staged = json.loads(line)
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success")
        self.assertIn("時間範圍: 2026-06-10 09:00-10:00 Asia/Taipei", staged["summary_text"])
        self.assertIn("參與者: author:9001, author:9002, author:9003, author:9004, author:9005", staged["summary_text"])
        self.assertIn("待回查線索:", staged["summary_text"])
        self.assertIn("author:9001 message 1", staged["summary_text"])
        self.assertEqual(manifest["fallback_counts"]["summaries"], 1)
        self.assertEqual(manifest["summary_llm_error_count"], 1)

    def test_taipei_day_boundary_selects_only_target_day_rows(self):
        rows = [
            raw_row_at(1, datetime(2026, 6, 9, 15, 59, tzinfo=timezone.utc)),
            raw_row_at(2, datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc)),
        ]
        fake_conn = FakeConnection(raw_rows=[rows[1]])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            line = (Path(result.staging_dir) / "summaries.jsonl").read_text(encoding="utf-8").splitlines()[0]
            staged = json.loads(line)

        self.assertEqual(result.status, "success")
        self.assertEqual(fake_conn.executions[0][1]["start_at"], datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(fake_conn.executions[0][1]["end_at"], datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(staged["summary_key"], "summary:hourly-v1:2026-06-10T00:00:00+08:00:2:2")

    def test_validation_failure_does_not_delete_or_insert_formal_summaries(self):
        fake_conn = FakeConnection(raw_rows=[raw_row(index, 9, index) for index in range(1, 4)])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(dimensions=2),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.committed_summaries, 0)
        self.assertEqual(manifest["error_count"], 1)
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertNotIn("DELETE FROM hourly_summaries", executed_sql)
        self.assertNotIn("INSERT INTO hourly_summaries", executed_sql)

    def test_successful_commit_replaces_same_day_strategy_and_keeps_stable_keys(self):
        rows = [raw_row(index, 9, index) for index in range(1, 4)]
        fake_conn = FakeConnection(raw_rows=rows, existing_batch_rows=[("summary-2026-06-10-r1",)])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_summaries_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            line = (Path(result.staging_dir) / "summaries.jsonl").read_text(encoding="utf-8").splitlines()[0]
            staged = json.loads(line)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.batch_id, "summary-2026-06-10-r2")
        self.assertEqual(staged["summary_key"], "summary:hourly-v1:2026-06-10T09:00:00+08:00:1:3")
        delete_execution = [
            execution for execution in fake_conn.executions if "DELETE FROM hourly_summaries" in execution[0]
        ][0]
        self.assertEqual(delete_execution[1]["strategy_version"], "hourly-v1")
        self.assertEqual(delete_execution[1]["start_at"], datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(delete_execution[1]["end_at"], datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc))

    def test_structured_logs_do_not_include_raw_or_prompt_text(self):
        fake_conn = FakeConnection(raw_rows=[raw_row(index, 9, index, "secret text") for index in range(1, 4)])
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(stderr):
                run_summaries_batch(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    embedding_provider=FakeEmbeddingProvider(),
                    settings=settings(Path(tmp)),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[-1]["status"], "success")
        self.assertIn("fallback_count", events[-1])
        self.assertNotIn("secret text", stderr.getvalue())

    def test_cli_run_batch_summaries_uses_summaries_pipeline(self):
        fake_result = mock.Mock()
        fake_result.status = "success"
        fake_result.to_json.return_value = json.dumps({"status": "success"})
        summary_provider = mock.Mock()

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli._embedding_provider_from_env", return_value=mock.Mock()):
                with mock.patch("src.cli._summary_provider_from_env", return_value=summary_provider):
                    with mock.patch("src.cli.run_summaries_batch", return_value=fake_result) as summaries_batch:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--only", "summaries"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "success"})
        summaries_batch.assert_called_once()
        self.assertEqual(summaries_batch.call_args.kwargs["day"], date(2026, 6, 10))
        self.assertIs(summaries_batch.call_args.kwargs["summary_provider"], summary_provider)

    def test_cli_all_marks_partial_failure_when_chunks_succeed_and_summaries_fail(self):
        chunks_result = mock.Mock()
        chunks_result.status = "success"
        chunks_result.batch_id = "chunk-2026-06-10-r1"
        chunks_result.staging_dir = "/tmp/chunk-staging"
        chunks_result.to_json.return_value = json.dumps({"status": "success", "batch_id": chunks_result.batch_id})
        summaries_result = mock.Mock()
        summaries_result.status = "failed"
        summaries_result.batch_id = "summary-2026-06-10-r1"
        summaries_result.staging_dir = ""
        summaries_result.to_json.return_value = json.dumps({"status": "failed", "batch_id": summaries_result.batch_id})

        with tempfile.TemporaryDirectory() as tmp:
            summaries_result.staging_dir = str(Path(tmp) / "2026-06-10" / summaries_result.batch_id)
            with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
                with mock.patch("src.cli._embedding_provider_from_env", return_value=mock.Mock()):
                    with mock.patch("src.cli._summary_provider_from_env", return_value=mock.Mock()):
                        with mock.patch("src.cli.run_chunks_batch", return_value=chunks_result):
                            with mock.patch("src.cli.run_summaries_batch", return_value=summaries_result):
                                output = io.StringIO()
                                stderr = io.StringIO()
                                with redirect_stdout(output), redirect_stderr(stderr):
                                    exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--only", "all"])

            payload = json.loads(output.getvalue())
            all_manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))
            events = [json.loads(line) for line in stderr.getvalue().splitlines()]

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "partial_failed")
        self.assertEqual(all_manifest["status"], "partial_failed")
        self.assertEqual(all_manifest["step_status"], {"chunks": "success", "summaries": "failed"})
        self.assertEqual([event["event"] for event in events], ["batch_partial_failed", "batch_finished"])
        self.assertEqual(events[-1]["status"], "partial_failed")

    def test_cli_all_success_writes_combined_manifest(self):
        chunks_result = mock.Mock()
        chunks_result.status = "success"
        chunks_result.batch_id = "chunk-2026-06-10-r1"
        chunks_result.staging_dir = "/tmp/chunk-staging"
        chunks_result.to_json.return_value = json.dumps({"status": "success", "batch_id": chunks_result.batch_id})
        summaries_result = mock.Mock()
        summaries_result.status = "success"
        summaries_result.batch_id = "summary-2026-06-10-r1"
        summaries_result.staging_dir = ""
        summaries_result.to_json.return_value = json.dumps({"status": "success", "batch_id": summaries_result.batch_id})
        summary_provider = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            summaries_result.staging_dir = str(Path(tmp) / "2026-06-10" / summaries_result.batch_id)
            with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
                with mock.patch("src.cli._embedding_provider_from_env", return_value=mock.Mock()):
                    with mock.patch("src.cli._summary_provider_from_env", return_value=summary_provider):
                        with mock.patch("src.cli.run_chunks_batch", return_value=chunks_result):
                            with mock.patch("src.cli.run_summaries_batch", return_value=summaries_result) as summaries_batch:
                                output = io.StringIO()
                                with redirect_stdout(output):
                                    exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--only", "all"])

            payload = json.loads(output.getvalue())
            all_manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(all_manifest["step_status"], {"chunks": "success", "summaries": "success"})
        self.assertIs(summaries_batch.call_args.kwargs["summary_provider"], summary_provider)

    def test_openai_summary_provider_gate_uses_approved_env(self):
        with mock.patch.dict(
            "os.environ",
            {
                "BATCH_SUMMARY_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-test",
                "SUMMARY_MODEL": "gpt-5.4-mini",
                "BATCH_PROVIDER_TIMEOUT_SECONDS": "12.5",
            },
            clear=True,
        ):
            with mock.patch("src.cli.OpenAISummaryClient") as client:
                provider = cli._summary_provider_from_env()

        self.assertIs(provider, client.return_value)
        client.assert_called_once_with(
            api_key="sk-test",
            model="gpt-5.4-mini",
            timeout_seconds=12.5,
        )


if __name__ == "__main__":
    unittest.main()
