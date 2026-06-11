# Group Memory Bot

Discord 群組強化 AI bot 的最小 skeleton。

目前程式碼只保留 bot 啟動與 `@bot` mention 占位回覆。RAG schema、runtime request flow 與 batch pipeline 已完成文件收斂，但尚未實作到程式碼。

產品範圍收斂紀錄見 [docs/design/bot_llm.md](docs/design/bot_llm.md)。
RAG schema 設計見 [docs/design/ddl.md](docs/design/ddl.md) 與 [docs/design/rag_schema.sql](docs/design/rag_schema.sql)。
`@bot` mention runtime 請求流程見 [docs/design/runtime_flow.md](docs/design/runtime_flow.md)。
日終批次、歷史 backfill 與 observability 設計見 [docs/design/batch_pipeline.md](docs/design/batch_pipeline.md)。
第一批實作前合約見 [docs/design/pre_implementation_contracts.md](docs/design/pre_implementation_contracts.md)。

## 文件狀態

- 已收斂：產品邊界、schema、runtime flow、batch/backfill flow、model placeholder、prompt 合約、normalization / eligibility、migration / CLI / Compose 邊界、structured logs。
- 尚待人為選定：answer/router/fallback/summary concrete model IDs。
- 尚待實作：prompt 檔、normalization / eligibility function、migration command、CLI entrypoint、Docker Compose DB service、runtime RAG、batch/import。

## 本機啟動

```bash
cp .env.example .env
docker compose up --build
```

## 驗證

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test*.py'
./.venv/bin/python -m compileall src tests
```
