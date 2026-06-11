# Pre-implementation Contracts

本文件收斂第一批實作 issue 之前必須固定的合約。它不選定實際 answer / router / fallback / summary model id，也不實作 runtime、batch、DB、migration 或 provider calls。

## 1. Decision Table

| Gap | Contract |
|---|---|
| `ANSWER_MODEL` | 保持 `<to-be-selected>`；需由人明確選定後才可啟用 answer LLM calls。 |
| `FALLBACK_MODEL` | 保持 `<to-be-selected>`；需由人明確選定後才可啟用 fallback LLM calls。 |
| `ROUTER_MODEL` | 保持 `<to-be-selected>`；需由人明確選定後才可啟用 router LLM calls。 |
| `SUMMARY_MODEL` | 保持 `<to-be-selected>`；需由人明確選定後才可啟用 summary LLM calls。 |
| `EMBEDDING_MODEL` | 第一版固定 `text-embedding-3-small`，與 `vector(1536)` schema 耦合。 |
| Answer prompt | 第一版使用 repo 內固定文字檔，預設路徑為 `prompts/answer_system.md`；修改後重啟生效，不做 prompt 管理系統或 hot reload。 |
| Router prompt | 第一版使用 repo 內固定文字檔，預設路徑為 `prompts/router_should_retrieve.md`；修改後重啟生效。 |
| Summary prompt | 約束仍由 `batch_pipeline.md` 管理；第一版不新增獨立 prompt 管理系統。 |
| Normalization / eligibility | runtime Discord ingestion 與 JSONL import 必須共用同一套純函式規則；結果同時產生 `normalized_content` 與 `is_rag_eligible`。 |
| Migration command | schema 只能由明確 CLI command 套用；bot startup 與 Docker entrypoint 不自動 migration。 |
| CLI entrypoint | batch/import/migration 使用明確 CLI entrypoint，不內建在 Discord bot process。 |
| Docker Compose DB | 下一個 implementation slice 可以加入 PostgreSQL + pgvector service；不得同時改成 bot 自動 migration。 |
| Structured logs | runtime 與 batch 都輸出 structured JSON application logs；不新增 DB request log、usage table 或 `batch_runs` table。 |

## 2. Prompt Contracts

### Answer System Prompt

第一版 answer system prompt 必須包含以下行為合約：

- 明確說明自己是 Discord 群組裡的 AI bot，不是群組真人成員。
- 可以使用提供的群組背景與最近對話讓回答更貼近群組脈絡。
- 沒有提供群組背景或背景不足時，仍可用一般 AI 能力回答，但不得假裝知道群組歷史。
- 群組背景與一般知識衝突時，回答應優先採用群組背景；若不確定，需明確保留不確定性。
- 不把推測包裝成歷史事實；避免未被 context 支援的「某人說過」、「某人喜歡」、「某人常常」。
- 不做負面人格判斷、嘲諷式標籤或冒犯性歸類。
- 不在使用者可見回覆中暴露 RAG、chunk、embedding、summary、prompt 等技術詞。
- 不宣稱自己可驗證歷史、統計訊息、查證誰何時說過什麼，除非未來另有明確工具實作。
- 回覆應以使用者當前問題與最近 session 為主；retrieved context 只作背景補強。

Prompt file 可以直接用上述合約改寫成自然語言 system prompt，但不得加入未收斂的產品聲音、角色扮演人格、安全政策或引用格式。

### Router `should_retrieve` Prompt

Router prompt 的唯一目的，是判斷 active session 是否需要重新查 RAG。輸入固定包含：

- current user query。
- previous `last_rag_query`。
- 最近最多 `ROUTER_SESSION_TURNS` 個 session turns。

Router 輸出必須是 JSON object：

```json
{
  "should_retrieve": true,
  "reason": "short reason"
}
```

`should_retrieve=true` 的情況：

- 使用者換了新主題。
- 使用者要求更多群組背景、過去對話、成員脈絡或 inside joke。
- 目前問題明顯無法只靠上一輪 retrieval context 回答。
- 上一輪 `last_rag_query` 與 current user query 的資訊需求不同。

`should_retrieve=false` 的情況：

- 使用者只是追問、改寫、澄清、要求延伸上一輪回答。
- 使用者問一般 AI 問題，且不需要群組長期記憶。
- router timeout、API error 或 JSON parse error；runtime flow 已定義此時沿用既有 context。

Router 不得回答使用者問題，不得產生 Discord 可見文字，也不得決定模型、topK、prompt budget 或 session 更新策略。

## 3. Shared Normalization / Eligibility

runtime Discord ingestion 與 JSONL import 必須呼叫同一套規則，避免歷史資料與新訊息進入不同記憶邏輯。

### Input

Normalization input 至少包含：

- `message_id`
- `created_at`
- `author_id`
- `raw_content`
- `is_bot_author`
- 是否來自 DM、thread、非指定 guild、非主要 channel
- 是否包含本 bot mention
- attachments / stickers / embeds / reactions 是否存在的 metadata

### Output

Normalization output 固定包含：

- `normalized_content: str`
- `is_rag_eligible: bool`
- `eligibility_reason: str`

`eligibility_reason` 只供 log、test、debug 使用，不寫入目前 schema，除非未來另開 schema issue。

### Normalization Rules

- `raw_content` 以 Discord 原始 content 保存，不做覆寫。
- `normalized_content` 移除本 bot mention token、首尾空白與控制字元。
- 多個連續空白可壓成單一空白；換行可保留為換行或壓成空白，但 runtime 與 import 必須一致。
- 不翻譯、不摘要、不改寫語氣、不替換 author id。
- URL 保留原文；第一版不抓網頁標題或外部內容。
- custom emoji、純 unicode emoji、貼圖、附件只靠文字 content 判定；第一版不解析圖片或附件。

### Eligibility Rules

`is_rag_eligible=false`：

- bot authored message。
- DM、thread、非指定 guild、非主要 channel。
- 空 `normalized_content`。
- 真人 `@bot` mention 訊息。
- 純 mention、純 emoji、純貼圖、純附件，且沒有其他文字內容。
- Discord system message 或 importer 無法確認為真人文字訊息的資料。

`is_rag_eligible=true`：

- 指定 guild / channel 內真人非 mention 訊息。
- `normalized_content` 非空。
- 訊息文字可獨立作為群組記憶素材。

Normalization 不做品質評分、禁用詞清單、inside joke 清單、人格標籤、人工審核或 opt-out。

## 4. Migration / CLI / Compose Boundaries

### CLI Entrypoint

第一版實作可以新增單一 CLI entrypoint，例如：

```bash
python -m src.cli <command> [options]
```

允許的 command boundary：

- `migrate up`：套用 `docs/design/rag_schema.sql` 對應的 schema。
- `migrate check`：檢查 DB extension/table/index 是否符合第一版 schema。
- `import-jsonl --input-dir ...`
- `run-batch --date YYYY-MM-DD --only all|chunks|summaries [--dry-run]`
- `backfill --start YYYY-MM-DD --end YYYY-MM-DD --only all|chunks|summaries --resume [--dry-run]`
- `check-batch --date YYYY-MM-DD --json`
- `cleanup-batches --older-than-days N`

實作時可以調整 Python module path，但 command 語意不得把 bot runtime、batch、migration 混在同一個自動流程。

### Migration Boundary

- Bot startup 不自動執行 migration。
- Docker entrypoint 不自動執行 migration。
- Migration command 不啟動 Discord bot。
- Batch/import command 不自動套 schema；schema 不存在時 fail fast。
- Migration 只處理第一版 schema，不同時做 backfill、embedding、summary 或 provider calls。

### Docker Compose Boundary

下一個 implementation slice 可以在 `docker-compose.yml` 加入 PostgreSQL + pgvector service，供本機開發與測試使用。

Compose service boundary：

- 可使用 pgvector image。
- 可提供 healthcheck。
- 可讓 bot service 透過 `DATABASE_URL` 連線。
- 不在 DB service 或 bot service entrypoint 自動套用 `rag_schema.sql`。
- 不加入外部向量資料庫、dashboard、scheduler、admin UI 或 production deployment 設定。

## 5. Structured JSON Logs

所有 application logs 採單行 JSON。欄位值應可被離線 usage 彙整腳本處理，但第一版不新增 DB usage table。

### Common Required Fields

- `event`
- `request_id` 或 `run_id`
- `timestamp`
- `level`
- `component`
- `status`
- `duration_ms`

### Runtime Request Log Fields

- `guild_id`
- `channel_id`
- `message_id`
- `session_id`
- `is_degraded`
- `router_decision`
- `retrieval_status`
- `retrieved_chunk_keys`
- `retrieved_summary_keys`
- `answer_model`
- `fallback_model`
- `router_model`
- `embedding_model`
- `token_input`
- `token_output`
- `estimated_cost`
- `failure_flags`

`request_id` 必須貫穿 raw upsert、session、retrieval、LLM call、Discord send 與 final request log。

### Batch Log Fields

Batch structured log 欄位以 `batch_pipeline.md` 的 Observability section 為準，並必須包含 model、token usage、estimated cost、retry count、error count 與 step status。

### Prohibited Log Content

Runtime logs、batch logs、manifest 與 `errors.jsonl` 都不得寫入：

- 完整 prompt。
- 完整 retrieved text。
- 完整 `raw_content` 或 `normalized_content`。
- Discord token、OpenAI API key、DB password 或完整 connection string。
- 未截斷的 provider request / response body。

允許記錄 metadata、artifact keys、message ids、hour/day、狀態摘要、短錯誤摘要、provider request id / response id。

## 6. First Implementation Slice Readiness

完成本文件後，後續 implementation issue 可拆成較小切片：

1. Config validation + prompt file existence checks。
2. Shared normalization / eligibility function and tests。
3. Explicit migration CLI。
4. Local PostgreSQL + pgvector Compose service。
5. Runtime raw message upsert without RAG。
6. Batch import command。

每個 implementation issue 仍需自己的 in scope / out of scope / acceptance criteria，不得直接把本清單視為一次全做。
