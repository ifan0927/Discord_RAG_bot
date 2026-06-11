# 群組強化 AI Bot 產品範圍收斂

這份文件是目前的產品收斂紀錄。它整理第一版產品方向與邊界，避免後續討論回到已排除範圍。

目前程式碼只保留最小 Discord bot skeleton；schema、runtime request flow、batch pipeline 與 bounded trial scope 已另行收斂，但尚未實作。本文只保存產品邊界、已排除範圍與文件索引；技術細節以 `ddl.md`、`runtime_flow.md`、`batch_pipeline.md` 與 `rag_schema.sql` 為準。

## 1. 產品定位

- 第一版目標是 Discord 群組強化 AI bot。
- 它是 AI bot，不是舊喝水小遊戲。
- 它是 bot，不模仿真人成員，也不假裝參與過過去事件。
- 它可以回答一般 AI 問題；即使群組記憶沒有命中，也可以一般回答。
- 它未來可以利用群組歷史文字紀錄，讓回答比一般網頁 AI 更有群組脈絡。
- 當群組記憶與一般 AI 知識衝突時，產品方向上優先採用群組記憶。
- 第一版不是可驗證歷史查詢工具，也不保證能正確回答統計或「誰何時說過什麼」。

## 2. 互動邊界

- 主要入口是 `@bot` mention。
- 只支援單一 guild / 單一主要 channel。
- 不支援 DM。
- 第一版 runtime 忽略 Discord threads；schema 內保留 `sessions.thread_id` 只是資料結構預留，不代表第一版產品支援 thread 互動。
- 不做主動插話、主動摘要、每日報告或定時報告。
- 目前 skeleton 不保留 slash command；若未來要恢復 `/usage` 或其他管理入口，需重新收斂後再落地。

## 3. 回覆邊界

- 必須維持明確 bot 身份。
- 可以有群組感、inside joke 與群組語氣，但不能冒犯成員。
- 不做負面人格判斷、嘲諷式標籤或冒犯性歸類。
- 可以提到具體成員名稱，但不能把推測包裝成歷史事實。
- 避免「某人說過」、「某人喜歡」、「某人常常」這類需要驗證的句子。
- 不在使用者回覆中暴露 RAG、chunk、embedding、summary 等技術詞。
- 回覆風格與安全邊界未來應可調整；第一版會用 repo 內固定 prompt 檔，不先做 prompt 管理系統或熱更新。

## 4. 資料與記憶邊界

- 歷史來源預期是既有 Discord JSONL exports，匯入後以資料庫 `raw_messages` 作為 source-of-truth。
- 未來正式運行時不長期依賴讀 JSONL 檔案；JSONL 只作為 import / backfill 輸入。
- 第一版只處理文字對話紀錄。
- 不處理圖片、附件、Discord 貼圖、表情貼或圖片理解。
- 不補 reactions、reply chain 或互動圖譜。
- 所有 bot authored message 都忽略，不寫入 raw、不進 RAG。
- 真人 `@bot` mention 訊息可以寫入 raw archive，但一律 `is_rag_eligible=false`，避免使用者提問本身污染長期群組記憶。
- 假設群組成員同意收錄；不做 opt-out。
- 需要基本規則式排除干擾訊息，例如其他 bot、空訊息、純 mention、純貼圖/表情；具體 normalization / eligibility 規則見 `runtime_flow.md`。
- 不做複雜品質分類、人工標註、人工審核、禁用詞清單或 inside joke 清單。

## 5. RAG 與上下文邊界

- 第一版方向是 chat mode，不做獨立 tool mode。
- 可驗證查詢、統計分析、誰何時說過什麼等工具型能力明確排除。
- 未來可使用群組記憶強化聊天，但不應把它包裝成可驗證引用工具。
- 短期上下文使用 `sessions`；長期群組記憶使用 `raw_messages` 經 batch 產生的 `conversation_chunks` 與 `hourly_summaries`。
- 新訊息 runtime 只即時寫入 raw；embedding、chunk、summary 由日終 batch 產生。
- retrieval 採 summary-first hybrid：先查 `hourly_summaries`，再取時間對齊 chunks，另以 global chunks 作為 summary miss fallback。
- active session 預設沿用上一輪 retrieval context，只有 router 判定需要時才重查 RAG；session DB、RAG 或 retrieval partial failure 可降級成一般 AI 回答。

## 6. 工程與成本邊界

- embedding model 固定為 `text-embedding-3-small`；answer/router/fallback/summary 模型保持 `<to-be-selected>`，必須由人明確選定後才可啟用對應 LLM calls。
- answer/router prompt 會以 repo 內固定文字檔管理；prompt 合約見 `runtime_flow.md`。summary prompt 約束由 `batch_pipeline.md` 記錄，第一版不做 prompt 管理系統或熱更新。
- runtime context window、檢索數量、timeout 與 token 上限已在 `runtime_flow.md` 收斂為環境變數。
- 第一版成本與用量觀察依 structured JSON logs 離線彙整，不先提供 Discord 內 `/usage`。
- 第一版 bounded trial 不做完整歷史 backfill；試跑範圍、API policy、dry-run / cost gate 與成功條件由 `batch_pipeline.md` 管理。
- request log 與 batch log 需要記錄模型名稱、token usage、cost metadata 與 failure flags；不新增 DB request log、usage table 或 `batch_runs` table。
- request log / errors log 禁止寫完整 prompt、retrieved text 或 raw content，只記 metadata、keys、狀態摘要與錯誤摘要。
- fallback 策略已收斂：RAG/session 失敗可一般回答，LLM/API 最終失敗才回固定錯誤文案。
- 不做離線評測、測試問題集或品質 benchmark；品質主要靠實際群組使用與 logs 觀察。
- 仍保留基本單元測試，保障程式骨架品質。

## 7. 目前程式碼邊界

- 只保留最小 Discord bot skeleton。
- 只保留 bot 啟動、guild/channel 限定與 mention 占位回覆。
- 尚未實作資料庫、schema migration、索引、session、模型 provider、prompt 檔、用量彙整或 slash command。
- Docker 目前只保留 bot service，不保留資料庫 service；下一個 implementation slice 可以加入 PostgreSQL + pgvector service，但 bot startup 與 Docker entrypoint 仍不得自動執行 migration。
- 舊喝水小遊戲功能、文件、資料表與資料都不保留。

## 8. 明確排除

- 舊喝水小遊戲。
- 可驗證 tool mode。
- slash command 產品入口。
- 歷史統計分析。
- 圖片、附件、貼圖、表情貼處理。
- reactions、reply chain、互動圖譜。
- daily summary、weekly summary、topic timeline。
- dashboard、Web UI、管理後台。
- 主動摘要、主動報告、主動插話。
- 多 guild、多 channel、多租戶權限。
- 人工標註、人工審核、禁用詞清單、inside joke 清單。
- 離線評測集或品質 benchmark。

## 9. 已收斂的技術文件

- `ddl.md`：RAG schema 決策與欄位理由。
- `rag_schema.sql`：第一版 PostgreSQL + pgvector DDL。
- `runtime_flow.md`：`@bot` mention request flow、session、retrieval、fallback、設定參數。
- `batch_pipeline.md`：JSONL import、日終 batch、backfill、staging、observability。

## 10. 實作前合約狀態

- answer / fallback / router / summary model 名稱：保持 `<to-be-selected>`，需由人明確選定；bounded trial 第一輪 real batch 只允許 `text-embedding-3-small` embedding，summary LLM 與 runtime LLM calls 仍 blocked。
- answer system prompt 與 router prompt：合約已收斂於 `runtime_flow.md`；實際 prompt 檔尚未實作。
- JSONL import 與 Discord runtime 共用的 normalization / eligibility 規則：已收斂於 `runtime_flow.md`。
- migration command 與 CLI entrypoint 邊界：已收斂於 `batch_pipeline.md`。
- bounded trial selection rule：raw DB 內最新 30 個完整 Asia/Taipei 日，不足 30 日則使用所有可用完整日；必須先通過 dry-run 與 cost gate。
- Docker Compose：下一個 implementation slice 可加入 PostgreSQL + pgvector service；不得自動 migration。
- structured logs：runtime log 合約見 `runtime_flow.md`；batch log、manifest 與 `errors.jsonl` 合約見 `batch_pipeline.md`。
