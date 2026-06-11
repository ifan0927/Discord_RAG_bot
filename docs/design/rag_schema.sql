CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE raw_messages (
  message_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL,
  author_id BIGINT NOT NULL,
  raw_content TEXT NOT NULL,
  normalized_content TEXT NOT NULL,
  is_rag_eligible BOOLEAN NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source TEXT NOT NULL CHECK (
    source IN ('jsonl_export', 'discord_gateway', 'discord_backfill')
  )
);

CREATE INDEX raw_messages_created_at_idx
  ON raw_messages (created_at);

CREATE INDEX raw_messages_rag_created_at_idx
  ON raw_messages (created_at)
  WHERE is_rag_eligible;

COMMENT ON TABLE raw_messages IS
  'Discord 原始訊息 archive，作為 raw source-of-truth。';
COMMENT ON COLUMN raw_messages.message_id IS
  'Discord snowflake message id，以 BIGINT 儲存。';
COMMENT ON COLUMN raw_messages.created_at IS
  'Discord message 建立時間，canonical event time。';
COMMENT ON COLUMN raw_messages.author_id IS
  'Discord author snowflake id；不在 raw 表保存使用者名稱快照。';
COMMENT ON COLUMN raw_messages.raw_content IS
  'Discord 原始文字內容。';
COMMENT ON COLUMN raw_messages.normalized_content IS
  '第一版 RAG 可用的清洗後文字，可為空字串。';
COMMENT ON COLUMN raw_messages.is_rag_eligible IS
  '是否可進入 chunk / summary / embedding pipeline。';
COMMENT ON COLUMN raw_messages.last_seen_at IS
  '最後一次由 importer / runtime upsert 看見此訊息的時間。';
COMMENT ON COLUMN raw_messages.source IS
  '最後一次看見此訊息的來源。';

CREATE TABLE conversation_chunks (
  chunk_key TEXT PRIMARY KEY,
  chunk_strategy_version TEXT NOT NULL,
  start_message_id BIGINT NOT NULL REFERENCES raw_messages(message_id),
  end_message_id BIGINT NOT NULL REFERENCES raw_messages(message_id),
  start_at TIMESTAMPTZ NOT NULL,
  end_at TIMESTAMPTZ NOT NULL,
  message_count INT NOT NULL CHECK (message_count > 0),
  chunk_text TEXT NOT NULL,
  embedding vector(1536) NOT NULL,
  embedding_model TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  batch_id TEXT NOT NULL,
  CHECK (start_at <= end_at),
  CHECK (start_message_id <= end_message_id)
);

CREATE INDEX conversation_chunks_embedding_hnsw_idx
  ON conversation_chunks
  USING hnsw (embedding vector_cosine_ops);

CREATE INDEX conversation_chunks_strategy_start_at_idx
  ON conversation_chunks (chunk_strategy_version, start_at);

CREATE INDEX conversation_chunks_batch_id_idx
  ON conversation_chunks (batch_id);

COMMENT ON TABLE conversation_chunks IS
  '時間連續對話 chunk 與其 embedding，作為主要 RAG retrieval 單位。';
COMMENT ON COLUMN conversation_chunks.chunk_key IS
  'Deterministic key，應包含 boundary 與 chunk_strategy_version。';
COMMENT ON COLUMN conversation_chunks.chunk_strategy_version IS
  'Chunk strategy version；所有 chunk retrieval query 都必須明確 filter。';
COMMENT ON COLUMN conversation_chunks.start_message_id IS
  'Chunk 起始 raw message id。';
COMMENT ON COLUMN conversation_chunks.end_message_id IS
  'Chunk 結束 raw message id。';
COMMENT ON COLUMN conversation_chunks.start_at IS
  'Chunk 起始時間，由 boundary raw message 衍生。';
COMMENT ON COLUMN conversation_chunks.end_at IS
  'Chunk 結束時間，由 boundary raw message 衍生。';
COMMENT ON COLUMN conversation_chunks.message_count IS
  'Chunk 內訊息數；不保存 token_count。';
COMMENT ON COLUMN conversation_chunks.chunk_text IS
  '實際送進 embedding model 的完整文字，也可直接放入 prompt。';
COMMENT ON COLUMN conversation_chunks.embedding IS
  'text-embedding-3-small 向量，維度 1536。';
COMMENT ON COLUMN conversation_chunks.embedding_model IS
  '第一版使用 text-embedding-3-small。';
COMMENT ON COLUMN conversation_chunks.generated_at IS
  '此 chunk artifact 的產生時間。';
COMMENT ON COLUMN conversation_chunks.batch_id IS
  '日終批次 ID，例如 chunk-2026-06-10-r1。';

CREATE TABLE hourly_summaries (
  summary_key TEXT PRIMARY KEY,
  summary_strategy_version TEXT NOT NULL,
  hour_start TIMESTAMPTZ NOT NULL,
  hour_end TIMESTAMPTZ NOT NULL,
  start_message_id BIGINT NOT NULL REFERENCES raw_messages(message_id),
  end_message_id BIGINT NOT NULL REFERENCES raw_messages(message_id),
  summary_text TEXT NOT NULL,
  embedding vector(1536) NOT NULL,
  embedding_model TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  batch_id TEXT NOT NULL,
  CHECK (hour_start < hour_end),
  CHECK (start_message_id <= end_message_id)
);

CREATE INDEX hourly_summaries_embedding_hnsw_idx
  ON hourly_summaries
  USING hnsw (embedding vector_cosine_ops);

CREATE INDEX hourly_summaries_strategy_hour_start_idx
  ON hourly_summaries (summary_strategy_version, hour_start);

CREATE INDEX hourly_summaries_batch_id_idx
  ON hourly_summaries (batch_id);

COMMENT ON TABLE hourly_summaries IS
  'Asia/Taipei 小時切分的 retrieval summary；無 eligible 訊息的小時不建 row。';
COMMENT ON COLUMN hourly_summaries.summary_key IS
  'Deterministic key，應包含 hour_start 與 summary_strategy_version。';
COMMENT ON COLUMN hourly_summaries.summary_strategy_version IS
  'Summary strategy version；所有 summary retrieval query 都必須明確 filter。';
COMMENT ON COLUMN hourly_summaries.hour_start IS
  'Asia/Taipei 小時半開區間起點。';
COMMENT ON COLUMN hourly_summaries.hour_end IS
  'Asia/Taipei 小時半開區間終點，不包含此時間。';
COMMENT ON COLUMN hourly_summaries.start_message_id IS
  '該小時摘要實際涵蓋的起始 eligible raw message id。';
COMMENT ON COLUMN hourly_summaries.end_message_id IS
  '該小時摘要實際涵蓋的結束 eligible raw message id。';
COMMENT ON COLUMN hourly_summaries.summary_text IS
  '實際送 embedding 與 retrieval prompt 使用的摘要文字。';
COMMENT ON COLUMN hourly_summaries.embedding IS
  'text-embedding-3-small 向量，維度 1536。';
COMMENT ON COLUMN hourly_summaries.embedding_model IS
  '第一版使用 text-embedding-3-small。';
COMMENT ON COLUMN hourly_summaries.generated_at IS
  '此 summary artifact 的產生時間。';
COMMENT ON COLUMN hourly_summaries.batch_id IS
  '日終批次 ID，例如 summary-2026-06-10-r1。';

CREATE TABLE sessions (
  session_id UUID PRIMARY KEY,
  channel_id BIGINT NOT NULL,
  thread_id BIGINT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_interaction_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  turns JSONB NOT NULL DEFAULT '[]'::jsonb,
  retrieved_chunk_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  retrieved_summary_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  last_rag_query TEXT,
  CHECK (expires_at > started_at),
  CHECK (last_interaction_at >= started_at),
  CHECK (jsonb_typeof(turns) = 'array')
);

CREATE INDEX sessions_channel_thread_expires_at_idx
  ON sessions (channel_id, thread_id, expires_at DESC);

COMMENT ON TABLE sessions IS
  '短期多輪對話 session；未過期時預設沿用上一輪 RAG context，不重查 RAG。';
COMMENT ON COLUMN sessions.session_id IS
  'Session UUID；由應用層產生。';
COMMENT ON COLUMN sessions.channel_id IS
  'Discord channel id。';
COMMENT ON COLUMN sessions.thread_id IS
  'Discord thread id；無 thread 時為 NULL。';
COMMENT ON COLUMN sessions.started_at IS
  'Session 開始時間。';
COMMENT ON COLUMN sessions.last_interaction_at IS
  '最後一次 session 互動時間。';
COMMENT ON COLUMN sessions.expires_at IS
  'Session 過期時間；active 判斷使用 expires_at > now()。';
COMMENT ON COLUMN sessions.turns IS
  'JSONB array；元素包含 role, message_id string/null, content, created_at。';
COMMENT ON COLUMN sessions.retrieved_chunk_keys IS
  '上一輪 RAG 命中的 conversation_chunks.chunk_key，無 FK。';
COMMENT ON COLUMN sessions.retrieved_summary_keys IS
  '上一輪 RAG 命中的 hourly_summaries.summary_key，無 FK。';
COMMENT ON COLUMN sessions.last_rag_query IS
  '上一輪實際用於 retrieval 的 query；只有重查 RAG 時更新。';
