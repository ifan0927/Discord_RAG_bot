# Group Memory Bot

Discord 群組強化 AI bot。

目前程式碼已包含 bot 啟動、`@bot` mention runtime orchestration、raw message upsert、session/RAG retrieval/LLM provider 邊界，以及 batch dry-run/chunks/summaries slices。真 Discord / OpenAI 驗證仍屬 ops / validation。

產品範圍收斂紀錄見 [docs/design/bot_llm.md](docs/design/bot_llm.md)。
RAG schema 設計見 [docs/design/ddl.md](docs/design/ddl.md) 與 [docs/design/rag_schema.sql](docs/design/rag_schema.sql)。
`@bot` mention runtime 請求流程見 [docs/design/runtime_flow.md](docs/design/runtime_flow.md)。
日終批次、歷史 backfill 與 observability 設計見 [docs/design/batch_pipeline.md](docs/design/batch_pipeline.md)。

## 文件狀態

- 已收斂：產品邊界、schema、runtime flow、batch/backfill flow、bounded trial selection rule、trial model IDs、prompt 合約、normalization / eligibility、migration / CLI / Compose 邊界、structured logs。
- 試跑 gate：最新 30 個完整 Asia/Taipei 日；第一輪 real batch 允許 embedding API 與 summary LLM；必須先通過 dry-run 與成本上限。
- 已批准模型：answer、fallback、router、summary 使用 `gpt-5.4-mini`；embedding 使用 `text-embedding-3-small`。
- 尚待實作：cleanup command，以及真 Discord / OpenAI ops validation。

## 本機啟動

```bash
cp .env.example .env
docker compose up --build
```

## Schema migration

Schema 只會透過明確指令套用；bot startup 與 Docker entrypoint 不會自動執行 migration。

```bash
docker compose up -d db
./.venv/bin/python -m src.cli migrate up
./.venv/bin/python -m src.cli migrate check
```

## JSONL import

歷史 JSONL 匯入只寫入 `raw_messages`，不產生 chunks、summaries、embeddings 或 LLM/provider calls。

```bash
./.venv/bin/python -m src.cli import-jsonl --input-dir exports/channel_123 --bot-user-id 999
```

## Bounded trial

Real trial 必須先跑 dry-run 並通過成本 gate。只有明確授權的 ops / validation trial 才可將 batch provider 設為 OpenAI。

```bash
./.venv/bin/python -m src.cli backfill --start YYYY-MM-DD --end YYYY-MM-DD --only all --dry-run
BATCH_EMBEDDING_PROVIDER=openai BATCH_SUMMARY_PROVIDER=openai ./.venv/bin/python -m src.cli backfill --start YYYY-MM-DD --end YYYY-MM-DD --only all
./.venv/bin/python -m src.cli check-batch --date YYYY-MM-DD --json
```

## 驗證

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test*.py'
./.venv/bin/python -m compileall src tests
```
