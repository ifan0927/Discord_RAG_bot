from __future__ import annotations

import json
import unittest
from unittest import mock

from src.openai_runtime import (
    OpenAIEmbeddingBatchClient,
    OpenAIResponsesClient,
    OpenAISummaryClient,
)
from src.runtime import LLMResult


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class OpenAIRuntimeTest(unittest.TestCase):
    def test_responses_client_extracts_answer_usage(self):
        payload = {
            "output_text": "answer",
            "usage": {
                "input_tokens": 123,
                "output_tokens": 45,
            },
        }

        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(payload)):
            result = OpenAIResponsesClient("sk-test").answer(
                "prompt",
                model="gpt-5.4-mini",
                timeout_seconds=12.5,
                max_output_tokens=300,
            )

        self.assertEqual(result.text, "answer")
        self.assertEqual(result.model, "gpt-5.4-mini")
        self.assertEqual(result.token_input, 123)
        self.assertEqual(result.token_output, 45)

    def test_embedding_batch_client_returns_embeddings_in_input_order(self):
        payload = {
            "data": [
                {"index": 1, "embedding": [0.2, 0.3]},
                {"index": 0, "embedding": [0.0, 0.1]},
            ]
        }

        with mock.patch("urllib.request.urlopen", return_value=FakeHttpResponse(payload)) as urlopen:
            result = OpenAIEmbeddingBatchClient(
                api_key="sk-test",
                model="text-embedding-3-small",
                timeout_seconds=12.5,
            ).embed(["first", "second"])

        self.assertEqual(result, [[0.0, 0.1], [0.2, 0.3]])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12.5)

    def test_summary_client_uses_responses_client_model_and_timeout(self):
        with mock.patch("src.openai_runtime.OpenAIResponsesClient") as responses:
            responses.return_value.answer.return_value = LLMResult(text="summary", model="gpt-5.4-mini")
            result = OpenAISummaryClient(
                api_key="sk-test",
                model="gpt-5.4-mini",
                timeout_seconds=12.5,
            ).summarize("input text", max_output_tokens=300)

        self.assertEqual(result, "summary")
        responses.return_value.answer.assert_called_once_with(
            "input text",
            model="gpt-5.4-mini",
            timeout_seconds=12.5,
            max_output_tokens=300,
        )


if __name__ == "__main__":
    unittest.main()
