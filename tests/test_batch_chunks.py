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
from src.batch_chunks import ChunkBatchSettings, run_chunks_batch


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


def settings(root: Path, **overrides):
    values = {
        "chunk_strategy_version": "timegap-v1",
        "embedding_model": "text-embedding-3-small",
        "chunk_window_messages": 20,
        "chunk_stride_messages": 10,
        "min_chunk_messages": 5,
        "chunk_text_max_chars": 8000,
        "batch_staging_dir": root,
    }
    values.update(overrides)
    return ChunkBatchSettings(**values)


def raw_row(message_id: int, minute: int = 0):
    return (
        message_id,
        datetime(2026, 6, 9, 16, minute, tzinfo=timezone.utc),
        9000 + message_id,
        f"message {message_id}",
    )


class BatchChunksTest(unittest.TestCase):
    def test_zero_eligible_day_stages_success_empty_and_commits_delete_only(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "success_empty")
        self.assertEqual(manifest["status"], "success_empty")
        self.assertEqual(manifest["input_raw_counts"], {"eligible": 0})
        self.assertEqual(manifest["step_status"], {"chunks": "success_empty"})
        self.assertEqual(result.staged_chunks, 0)
        self.assertEqual(result.committed_chunks, 0)
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertIn("DELETE FROM conversation_chunks", executed_sql)
        self.assertNotIn("INSERT INTO conversation_chunks", executed_sql)

    def test_tail_under_minimum_does_not_create_extra_chunk(self):
        rows = [raw_row(index, index) for index in range(1, 25)]
        provider = FakeEmbeddingProvider()
        fake_conn = FakeConnection(raw_rows=rows)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=provider,
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.staged_chunks, 2)
        self.assertEqual(len(provider.calls[0]), 2)

    def test_validation_failure_does_not_delete_or_insert_formal_chunks(self):
        fake_conn = FakeConnection(raw_rows=[raw_row(index, index) for index in range(1, 6)])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(dimensions=2),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.committed_chunks, 0)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error_count"], 1)
        self.assertEqual(manifest["staging_file_paths"]["chunks.jsonl"].endswith("chunks.jsonl"), True)
        self.assertIn("chunks.jsonl", manifest["staging_file_sha256"])
        self.assertEqual(manifest["token_usage"]["chunk_embedding_input"], "unknown")
        self.assertEqual(manifest["estimated_cost"], "unknown")
        self.assertIn("embedding must have 1536 dimensions", manifest["errors"][0])
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertNotIn("DELETE FROM conversation_chunks", executed_sql)
        self.assertNotIn("INSERT INTO conversation_chunks", executed_sql)

    def test_embedding_count_mismatch_fails_validation_without_commit(self):
        class ExtraEmbeddingProvider:
            def embed(self, texts):
                return [[0.0] * 1536 for _text in texts] + [[1.0] * 1536]

        fake_conn = FakeConnection(raw_rows=[raw_row(index, index) for index in range(1, 6)])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=ExtraEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        self.assertEqual(result.status, "failed")
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertNotIn("DELETE FROM conversation_chunks", executed_sql)
        self.assertNotIn("INSERT INTO conversation_chunks", executed_sql)

    def test_chunk_count_mismatch_fails_validation_without_commit(self):
        fake_conn = FakeConnection(raw_rows=[raw_row(index, index) for index in range(1, 6)])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("src.batch_chunks._expected_chunk_count", return_value=2):
                result = run_chunks_batch(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    embedding_provider=FakeEmbeddingProvider(),
                    settings=settings(Path(tmp)),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(manifest["expected_chunks"], 2)
        self.assertEqual(manifest["actual_chunks"], 1)
        self.assertIn("chunk count mismatch: expected 2, actual 1", manifest["errors"])
        executed_sql = "\n".join(execution[0] for execution in fake_conn.executions)
        self.assertNotIn("DELETE FROM conversation_chunks", executed_sql)
        self.assertNotIn("INSERT INTO conversation_chunks", executed_sql)

    def test_chunks_batch_writes_structured_json_logs_to_stderr(self):
        fake_conn = FakeConnection(raw_rows=[raw_row(index, index) for index in range(1, 6)])
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(stderr):
                result = run_chunks_batch(
                    database_url="postgresql://local/test",
                    day=date(2026, 6, 10),
                    embedding_provider=FakeEmbeddingProvider(),
                    settings=settings(Path(tmp)),
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )

        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(result.status, "success")
        self.assertEqual(
            [event["event"] for event in events],
            [
                "batch_started",
                "batch_step_started",
                "artifact_committed",
                "batch_step_finished",
                "batch_finished",
            ],
        )
        self.assertEqual(events[-1]["status"], "success")
        self.assertEqual(events[-1]["batch_id"], result.batch_id)
        self.assertEqual(events[-1]["expected_count"], 1)
        self.assertEqual(events[-1]["actual_count"], 1)

    def test_failed_rerun_gets_new_batch_id_from_existing_staging(self):
        rows = [raw_row(index, index) for index in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10" / "chunk-2026-06-10-r1").mkdir(parents=True)
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(dimensions=2),
                settings=settings(root),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: FakeConnection(raw_rows=rows),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.batch_id, "chunk-2026-06-10-r2")

    def test_success_empty_rerun_gets_new_batch_id_from_existing_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10" / "chunk-2026-06-10-r1").mkdir(parents=True)
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(root),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: FakeConnection(),
            )

        self.assertEqual(result.status, "success_empty")
        self.assertEqual(result.batch_id, "chunk-2026-06-10-r2")

    def test_successful_commit_replaces_same_day_strategy_and_keeps_stable_keys(self):
        rows = [raw_row(index, index) for index in range(1, 16)]
        fake_conn = FakeConnection(raw_rows=rows, existing_batch_rows=[("chunk-2026-06-10-r1",)])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_chunks_batch(
                database_url="postgresql://local/test",
                day=date(2026, 6, 10),
                embedding_provider=FakeEmbeddingProvider(),
                settings=settings(Path(tmp)),
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

            chunk_lines = (Path(result.staging_dir) / "chunks.jsonl").read_text(encoding="utf-8").splitlines()

        staged = [json.loads(line) for line in chunk_lines]
        self.assertEqual(result.status, "success")
        self.assertEqual(result.batch_id, "chunk-2026-06-10-r2")
        self.assertEqual(
            [row["chunk_key"] for row in staged],
            ["chunk:timegap-v1:2026-06-10:1:15", "chunk:timegap-v1:2026-06-10:11:15"],
        )
        self.assertEqual(staged[0]["batch_id"], "chunk-2026-06-10-r2")

        delete_execution = [
            execution for execution in fake_conn.executions if "DELETE FROM conversation_chunks" in execution[0]
        ][0]
        self.assertEqual(delete_execution[1]["strategy_version"], "timegap-v1")
        self.assertEqual(delete_execution[1]["start_at"], datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(delete_execution[1]["end_at"], datetime(2026, 6, 10, 16, 0, tzinfo=timezone.utc))
        inserts = [execution for execution in fake_conn.executions if "INSERT INTO conversation_chunks" in execution[0]]
        self.assertEqual(len(inserts), 2)

    def test_cli_run_batch_chunks_uses_chunks_pipeline(self):
        fake_result = mock.Mock()
        fake_result.status = "success"
        fake_result.to_json.return_value = json.dumps({"status": "success"})

        with mock.patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}):
            with mock.patch("src.cli._embedding_provider_from_env", return_value=mock.Mock()):
                with mock.patch("src.cli.run_chunks_batch", return_value=fake_result) as chunks_batch:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--only", "chunks"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "success"})
        chunks_batch.assert_called_once()
        self.assertEqual(chunks_batch.call_args.kwargs["day"], date(2026, 6, 10))

    def test_cli_run_batch_chunks_supports_fake_provider_gate(self):
        fake_result = mock.Mock()
        fake_result.status = "success"
        fake_result.to_json.return_value = json.dumps({"status": "success"})

        with mock.patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://local/test", "BATCH_EMBEDDING_PROVIDER": "fake"},
        ):
            with mock.patch("src.cli.run_chunks_batch", return_value=fake_result) as chunks_batch:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli.main(["run-batch", "--date", "2026-06-10", "--only", "chunks"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "success"})
        provider = chunks_batch.call_args.kwargs["embedding_provider"]
        self.assertEqual(len(provider.embed(["same text"])[0]), 1536)


if __name__ == "__main__":
    unittest.main()
