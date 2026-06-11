# RAG Schema DDL 設計紀錄

本文件整理 Discord 群組 AI bot 第一版 RAG schema 的收斂結果，作為後續 implementation reference。實際 SQL DDL 見 `rag_schema.sql`。

## 固定背景

- 資料來源是 Discord 群組聊天。
- 歷史資料為 JSONL 日切 export，runtime 會即時收錄新訊息。
- 向量儲存使用 PostgreSQL + pgvector，不引入外部向量資料庫。
- 新訊息即時寫入 raw，embedding / chunk / summary 走日終批次。
- `@bot` mention 觸發查詢，RAG 檢索 `conversation_chunks` + `hourly_summaries`。
- session 內短期上下文不走 RAG；未過期 session 預設沿用上一輪 retrieval context。
- 第一版只處理文字，排除圖片、附件、貼圖、表情符號。
- 單一 guild、單一主要 channel。
- 群組約 4 人，歷史訊息約 60 萬則。
- 明確排除 reply_to / reactions、統計 dashboard、daily / weekly summary。

## 1. raw_messages

`raw_messages` 是 Discord 原始訊息 archive，也是後續 chunk / summary / embedding 的 source-of-truth。第一版不因 RAG eligibility 排除而丟棄 Discord 來源資料。

### 欄位決策

- `message_id BIGINT PRIMARY KEY`
  - Discord snowflake 以 `BIGINT` 儲存。
  - 不使用 `TEXT`，因為 ingestion 直接處理檔案與 Python runtime，不需要為 JS number 精度路徑犧牲 index / sort 效率。
  - 主鍵只使用 `message_id`，不使用 `(guild_id, channel_id, message_id)`；未來多 guild / channel 若需要，另以來源對照表處理。

- `created_at TIMESTAMPTZ NOT NULL`
  - canonical event time 來自 JSONL / Discord runtime 的 message 建立時間。
  - 不從 snowflake 推導時間；`message_id` 只作為穩定身份。

- `author_id BIGINT NOT NULL`
  - 不建 users 表，不存 author name 快照。
  - 4 人規模下 debug / 顯示可由外部固定對照表或程式轉換處理。

- `raw_content TEXT NOT NULL`
  - Discord 原始文字內容。

- `normalized_content TEXT NOT NULL`
  - 第一版 RAG 可用的清洗後文字。
  - 允許空字串，因為 raw 表要保留所有 Discord 來源訊息。

- `is_rag_eligible BOOLEAN NOT NULL`
  - 控制是否進入 chunk / summary / embedding pipeline。
  - 原始訊息存在與第一版 RAG 是否使用是兩件事。

- `last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()`
  - 這筆訊息最後一次由 importer / runtime upsert 看見的時間。

- `source TEXT NOT NULL`
  - 表示最後一次看見此訊息的來源。
  - 允許值固定為 `jsonl_export`、`discord_gateway`、`discord_backfill`。
  - 使用 CHECK constraint，避免來源欄位變成自由文字。

### Index

- `raw_messages_created_at_idx (created_at)`
  - 支援 raw archive 的時間區間掃描。

- `raw_messages_rag_created_at_idx (created_at) WHERE is_rag_eligible`
  - 支援日終 chunk / summary job 掃 eligible 訊息。
  - 60 萬則規模下額外 index 成本低，便利性高。

## 2. conversation_chunks

`conversation_chunks` 是主要 RAG retrieval 單位。`raw_messages` 是訊息原子，直接對 raw message 做 embedding 容易得到缺乏上下文的碎片；chunk 會把連續對話包成可檢索語境。

### 欄位決策

- `chunk_key TEXT PRIMARY KEY`
  - deterministic key，應包含 chunk boundary 與 `chunk_strategy_version`。
  - 避免使用純 `BIGSERIAL` 造成重跑 / diff / debug 時 identity 不穩。

- `chunk_strategy_version TEXT NOT NULL`
  - 版本字串帶策略語意，例如 `timegap-v1`。
  - 所有 chunk retrieval query 都必須明確 filter 此欄位。
  - 不設 `is_active`；current strategy 由應用層環境設定或部署設定控制。

- `start_message_id BIGINT NOT NULL`
- `end_message_id BIGINT NOT NULL`
  - 皆 FK 到 `raw_messages(message_id)`。
  - chunk 預設按時間連續切分，boundary 足以支援回溯與 debug。

- `start_at TIMESTAMPTZ NOT NULL`
- `end_at TIMESTAMPTZ NOT NULL`
  - chunk 自帶時間範圍，用於 retrieval filter、recency sort 與 debug。
  - 這是從 raw boundary 衍生的 chunk metadata。

- `message_count INT NOT NULL`
  - 描述 chunk 形狀，必須大於 0。
  - 不存 `token_count`，避免把 tokenizer / model 依賴混入 chunk schema。

- `chunk_text TEXT NOT NULL`
  - 實際送進 embedding model 的完整文字。
  - retrieval 命中後可直接回放與放入 prompt。

- `embedding vector(1536) NOT NULL`
  - 使用 OpenAI `text-embedding-3-small` 維度 1536。
  - 不支援同表多 embedding model A/B。

- `embedding_model TEXT NOT NULL`
  - 第一版值預期為 `text-embedding-3-small`。

- `generated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
  - 此 chunk artifact 的產生時間。

- `batch_id TEXT NOT NULL`
  - 日終批次 ID，例如 `chunk-2026-06-10-r1`。
  - 日期為主，但必須允許 run suffix，避免同一天重跑無法區分。

### Index

- HNSW index on `embedding vector_cosine_ops`
  - 查詢使用 cosine distance operator `<=>`。
  - 日終批次寫入、chunk 向量量級預期幾千到幾萬，HNSW 是第一版合理選擇。
  - 不使用 IVFFlat，避免不必要的 lists / probes 調參。

- `conversation_chunks_strategy_start_at_idx (chunk_strategy_version, start_at)`
  - 支援 strategy filter、時間範圍查詢與 debug。

- `conversation_chunks_batch_id_idx (batch_id)`
  - 支援批次檢查、刪除與重跑清理。

## 3. hourly_summaries

`hourly_summaries` 是 retrieval 用的時間段摘要，不是 daily / weekly summary。無 eligible 訊息的小時不建 row；查不到 summary 即代表沒有可用摘要。

### 欄位決策

- `summary_key TEXT PRIMARY KEY`
  - deterministic key，應包含 `hour_start` 與 `summary_strategy_version`。
  - hourly summary 資料量極小，key 較肥不是問題。

- `summary_strategy_version TEXT NOT NULL`
  - 摘要策略版本。

- `hour_start TIMESTAMPTZ NOT NULL`
- `hour_end TIMESTAMPTZ NOT NULL`
  - 使用 Asia/Taipei 小時切分。
  - 語意為半開區間 `[hour_start, hour_end)`。
  - 儲存為 `TIMESTAMPTZ`，實作必須嚴格用 Asia/Taipei 切 boundary。

- `start_message_id BIGINT NOT NULL`
- `end_message_id BIGINT NOT NULL`
  - 皆 FK 到 `raw_messages(message_id)`。
  - 表示該小時摘要實際涵蓋的 eligible 訊息範圍。

- `summary_text TEXT NOT NULL`
  - 實際送 embedding 與 retrieval prompt 使用的摘要文字。
  - 不拆 raw/generated summary；summary 的 source-of-truth 仍是 raw messages。

- `embedding vector(1536) NOT NULL`
  - 與 chunks 一樣使用 `text-embedding-3-small`。

- `embedding_model TEXT NOT NULL`
  - 第一版值預期為 `text-embedding-3-small`。

- `generated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
  - 此 summary artifact 的產生時間。

- `batch_id TEXT NOT NULL`
  - 日終批次 ID，例如 `summary-2026-06-10-r1`。
  - 與 chunk batch prefix 分開，方便排查。

### Index

- HNSW index on `embedding vector_cosine_ops`
  - 查詢使用 cosine distance operator `<=>`。

- `hourly_summaries_strategy_hour_start_idx (summary_strategy_version, hour_start)`
  - 支援 strategy filter、時間查詢與 debug。

- `hourly_summaries_batch_id_idx (batch_id)`
  - 支援批次檢查、刪除與重跑清理。

## 4. sessions

`sessions` 是短期多輪對話狀態，不是長期記憶、prompt log 或 raw Discord event archive。session 未過期時，`@bot` mention 預設不重查 RAG，而是沿用上一輪 retrieval context 與短期 turns。

### 欄位決策

- `session_id UUID PRIMARY KEY`
  - 由應用層產生。
  - 不使用 channel/thread 作為 PK，因為同一 channel/thread 會有多段歷史 session。

- `channel_id BIGINT NOT NULL`
  - Discord channel id。

- `thread_id BIGINT NULL`
  - Discord thread id；無 thread 時為 NULL。
  - 保留 thread 維度，支援 Discord thread / discussion 自然隔離。

- `started_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `last_interaction_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `expires_at TIMESTAMPTZ NOT NULL`
  - 不存 `status`。
  - active session 以 `expires_at > now()` 判斷。
  - 第一版沒有手動關閉 session 需求。

- `turns JSONB NOT NULL DEFAULT '[]'::jsonb`
  - 單表保存短期多輪上下文。
  - JSONB 僅保存 prompt 所需 turn 資訊，不作為 raw Discord event archive。
  - 每個元素最小結構：

```json
{
  "role": "user | assistant",
  "message_id": "discord message id as string or null",
  "content": "text used in prompt",
  "created_at": "ISO timestamp"
}
```

  - `message_id` 在 JSONB 裡使用 string 或 null，避免 Discord snowflake 在 JS / tooling 路徑中丟精度。

- `retrieved_chunk_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]`
  - 上一輪 RAG 命中的 `conversation_chunks.chunk_key`。
  - 不加 FK，因為 chunks 是可重建 artifact，session 是短期狀態。

- `retrieved_summary_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]`
  - 上一輪 RAG 命中的 `hourly_summaries.summary_key`。
  - 不加 FK，避免 session 反向綁死 summary artifact lifecycle。

- `last_rag_query TEXT`
  - 上一輪實際用於 retrieval 的 query。
  - 只有重查 RAG 時更新。
  - 讓 retrieved keys 具備可 debug provenance。

### Index

- `sessions_channel_thread_expires_at_idx (channel_id, thread_id, expires_at DESC)`
  - 支援查找同 channel/thread 未過期的最近 session。
  - 查詢 thread id 時應使用 `thread_id IS NOT DISTINCT FROM ?` 處理 NULL。
  - 不額外建立 `expires_at` 清理 index；第一版 session 資料量小，清理掃描成本可接受。

## Retrieval 約束

- chunk retrieval 必須 filter `chunk_strategy_version`。
- summary retrieval 必須 filter `summary_strategy_version`。
- chunk / summary vector search 使用 cosine distance `<=>`。
- session 未過期時預設不重查 RAG。
- session 若重查 RAG，必須更新：
  - `last_rag_query`
  - `retrieved_chunk_keys`
  - `retrieved_summary_keys`
  - `last_interaction_at`
  - `expires_at`

## Batch 約束

- `raw_messages` 可由 JSONL import、Discord gateway、Discord backfill upsert。
- 日終批次從 `raw_messages` 讀取 `is_rag_eligible = true` 的資料生成 chunks / hourly summaries。
- chunk batch id 使用 `chunk-YYYY-MM-DD-rN` 類型格式。
- summary batch id 使用 `summary-YYYY-MM-DD-rN` 類型格式。
- 同一天重跑必須增加 run suffix，不允許只用日期。

## 明確不做

- 不建 users 表。
- 不建多 guild / 多 channel schema。
- 不存 token_count。
- 不做 embedding model A/B 共存。
- 不設 `is_active` 版本切換欄位。
- 不為空小時建立 hourly summary row。
- 不把 session turns 正規化成第五張表。
- 不為 retrieved chunk / summary keys 建 FK 或 join table。
