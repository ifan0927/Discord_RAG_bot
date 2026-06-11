from datetime import datetime, timezone
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src import cli
from src.jsonl_import import import_jsonl_directory


class FakeSchema:
    def __init__(self, ok=True):
        self.ok = ok


class FakeConnection:
    def __init__(self):
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))


def write_jsonl(root: Path, relative_path: str, rows: list[dict]) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def row(**overrides):
    values = {
        "id": 1001,
        "guild_id": 10,
        "channel_id": 20,
        "author_id": 30,
        "created_at": "2026-06-11T12:00:00+00:00",
        "content": " hello   group\nagain ",
        "attachments": [],
        "embeds": [],
    }
    values.update(overrides)
    return values


class JsonlImportTest(unittest.TestCase):
    def test_import_upserts_raw_messages_with_shared_normalization(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(
                input_dir,
                "channel_20/2026/06/2026-06-11.jsonl",
                [
                    row(),
                    row(id=1002, content="<@999> question", author_id=31),
                    row(id=1003, content="bot text", author_id=32, author_is_bot=True),
                    row(id=1004, content="", author_id=33, attachments=[{"id": 1}]),
                ],
            )

            result = import_jsonl_directory(
                input_dir=input_dir,
                database_url="postgresql://local/test",
                guild_id=10,
                channel_id=20,
                bot_user_id=999,
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        self.assertEqual(result.files, 1)
        self.assertEqual(result.imported, 4)
        self.assertEqual(result.upserted, 4)
        self.assertEqual(result.eligible, 1)
        params = [execution[1] for execution in fake_conn.executions]
        self.assertEqual(params[0]["raw_content"], " hello   group\nagain ")
        self.assertEqual(params[0]["normalized_content"], "hello group again")
        self.assertTrue(params[0]["is_rag_eligible"])
        self.assertEqual(params[1]["normalized_content"], "question")
        self.assertFalse(params[1]["is_rag_eligible"])
        self.assertFalse(params[2]["is_rag_eligible"])
        self.assertFalse(params[3]["is_rag_eligible"])
        self.assertTrue(all("raw_messages" in execution[0] for execution in fake_conn.executions))
        self.assertTrue(all("conversation_chunks" not in execution[0] for execution in fake_conn.executions))
        self.assertTrue(all("hourly_summaries" not in execution[0] for execution in fake_conn.executions))

    def test_importing_same_fixture_twice_issues_same_upserts(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(input_dir, "2026-06-11.jsonl", [row()])

            for _ in range(2):
                result = import_jsonl_directory(
                    input_dir=input_dir,
                    database_url="postgresql://local/test",
                    guild_id=10,
                    channel_id=20,
                    bot_user_id=999,
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: fake_conn,
                )
                self.assertEqual(result.imported, 1)
                self.assertEqual(result.upserted, 1)

        self.assertEqual(len(fake_conn.executions), 2)
        self.assertIn("ON CONFLICT (message_id) DO UPDATE", fake_conn.executions[0][0])

    def test_string_false_metadata_does_not_mark_message_ineligible(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(
                input_dir,
                "2026-06-11.jsonl",
                [
                    row(
                        author_is_bot="false",
                        is_thread="false",
                        is_bot_mentioned="false",
                        is_system_message="false",
                    )
                ],
            )

            result = import_jsonl_directory(
                input_dir=input_dir,
                database_url="postgresql://local/test",
                guild_id=10,
                channel_id=20,
                bot_user_id=999,
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        self.assertEqual(result.eligible, 1)
        self.assertTrue(fake_conn.executions[0][1]["is_rag_eligible"])

    def test_import_fails_fast_when_schema_check_fails(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(input_dir, "2026-06-11.jsonl", [row()])

            with self.assertRaisesRegex(RuntimeError, "schema check failed"):
                import_jsonl_directory(
                    input_dir=input_dir,
                    database_url="postgresql://local/test",
                    guild_id=10,
                    channel_id=20,
                    schema_checker=lambda database_url: FakeSchema(ok=False),
                    connect=lambda database_url: fake_conn,
                )

        self.assertEqual(fake_conn.executions, [])

    def test_missing_required_jsonl_field_fails_without_body_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(
                input_dir,
                "2026-06-11.jsonl",
                [row(content="secret message body", author_id=None)],
            )

            with self.assertRaisesRegex(RuntimeError, "author_id"):
                import_jsonl_directory(
                    input_dir=input_dir,
                    database_url="postgresql://local/test",
                    guild_id=10,
                    channel_id=20,
                    schema_checker=lambda database_url: FakeSchema(ok=True),
                    connect=lambda database_url: FakeConnection(),
                )

    def test_cli_import_jsonl_prints_counts_without_message_bodies(self):
        fake_result = mock.Mock()
        fake_result.summary.return_value = "files=1 imported=1 upserted=1 eligible=1"

        with mock.patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql://local/test",
                "GUILD_ID": "10",
                "CHANNEL_ID": "20",
            },
            clear=True,
        ):
            with mock.patch("src.cli.import_jsonl_directory", return_value=fake_result) as importer:
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli.main(["import-jsonl", "--input-dir", "fixtures", "--bot-user-id", "999"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().strip(), "files=1 imported=1 upserted=1 eligible=1")
        self.assertNotIn("secret", output.getvalue())
        importer.assert_called_once()
        self.assertEqual(importer.call_args.kwargs["bot_user_id"], 999)

    def test_created_at_is_stored_as_utc_datetime(self):
        fake_conn = FakeConnection()
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp)
            write_jsonl(input_dir, "2026-06-11.jsonl", [row(created_at="2026-06-11T20:00:00+08:00")])

            import_jsonl_directory(
                input_dir=input_dir,
                database_url="postgresql://local/test",
                guild_id=10,
                channel_id=20,
                schema_checker=lambda database_url: FakeSchema(ok=True),
                connect=lambda database_url: fake_conn,
            )

        self.assertEqual(
            fake_conn.executions[0][1]["created_at"],
            datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
