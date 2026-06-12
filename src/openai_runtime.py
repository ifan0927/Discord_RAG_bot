from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.runtime import LLMResult


class OpenAIResponsesClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for runtime LLM calls")
        self.api_key = api_key

    def answer(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> LLMResult:
        payload = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        response = self._post(payload, timeout_seconds)
        text = _extract_output_text(response)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return LLMResult(
            text=text,
            model=model,
            token_input=usage.get("input_tokens"),
            token_output=usage.get("output_tokens"),
        )

    def should_retrieve(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> bool:
        result = self.answer(
            prompt,
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError:
            return False
        return bool(parsed.get("should_retrieve"))

    def _post(self, payload: dict, timeout_seconds: float) -> dict:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"OpenAI Responses API failed: {exc.code} {detail}") from exc
        return json.loads(body)


class OpenAIEmbeddingClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for runtime embedding calls")
        self.api_key = api_key

    def embed_query(self, query: str, *, model: str, timeout_seconds: float) -> list[float]:
        payload = {"model": model, "input": query}
        request = urllib.request.Request(
            "https://api.openai.com/v1/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"OpenAI Embeddings API failed: {exc.code} {detail}") from exc
        parsed = json.loads(body)
        return list(parsed["data"][0]["embedding"])


def _extract_output_text(response: dict) -> str:
    text = response.get("output_text")
    if isinstance(text, str) and text:
        return text
    chunks: list[str] = []
    for output in response.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks)
    raise RuntimeError("OpenAI Responses API returned no output text")
