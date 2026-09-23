-- AI 代理人操控風險演練平台：資料結構
-- 設計原則：累積性靠 drill_run 的多筆記錄，不做帳號系統。

CREATE TABLE IF NOT EXISTS organization (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 盤點出的 AI 應用。結構對齊數發部附錄一「AI 應用情境盤點表」
-- （1.1 應用場景描述 / 1.2 AI 技術 / 1.3 利害關係人）
CREATE TABLE IF NOT EXISTS ai_application (
    id            INTEGER PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES organization(id),
    name          TEXT NOT NULL,
    scenario_type TEXT NOT NULL,          -- recruit | procurement | support | research
    description   TEXT NOT NULL,          -- 自由文字，餵給 LLM 做風險對應
    ai_tech       TEXT,                   -- 對應盤點表 1.2
    stakeholders  TEXT,                   -- 對應盤點表 1.3
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 風險對應結果：官方 3 大類 20 項的代碼（A1..A8 / B1..B6 / C1..C6）
CREATE TABLE IF NOT EXISTS risk_mapping (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES ai_application(id),
    risk_code      TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 測試素材（一律為 LLM 生成的假資料，絕不使用真實履歷）
CREATE TABLE IF NOT EXISTS material (
    id                   INTEGER PRIMARY KEY,
    scenario_type        TEXT NOT NULL,
    -- 含注入插入點 {{INJECTION}} 的素材原文；無注入時該處替換為空字串
    content              TEXT NOT NULL,
    -- ground truth 不可自行標註，須為多個模型在乾淨版下的一致排序（JSON 陣列）
    ground_truth_ranking TEXT,
    gt_verified          INTEGER NOT NULL DEFAULT 0,  -- 一致性驗證是否通過
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 注入樣本
CREATE TABLE IF NOT EXISTS injection (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,   -- override | authority | suppress_compare | exfiltrate | score
    visibility TEXT NOT NULL,   -- hidden（淨化器可處理）| plain（不可處理）
    carrier    TEXT NOT NULL,   -- white_text | zero_font | metadata | html_comment | body
    strength   INTEGER NOT NULL,-- 1..4 指令性階梯（與 visibility 正交，勿混用）
    payload    TEXT NOT NULL,
    target     TEXT,            -- 注入想推捧的對象代號；判定是否被操控的依據
    source     TEXT NOT NULL DEFAULT 'seed',  -- seed=人工種子 | redteam=LLM 對抗式生成
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 一次演練批次
CREATE TABLE IF NOT EXISTS drill_run (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES ai_application(id),
    status         TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed
    configs        TEXT NOT NULL,   -- JSON 陣列，如 ["A","B","C","D"]
    models         TEXT NOT NULL,   -- JSON 陣列
    total_cases    INTEGER NOT NULL DEFAULT 0,
    done_cases     INTEGER NOT NULL DEFAULT 0,
    est_usd        REAL,            -- 建立時的事前估算，供成本閘門判斷
    actual_usd     REAL NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    started_at     TEXT,
    finished_at    TEXT
);

-- 單一測試案例：四配置 × 模型 × 素材 × 注入
CREATE TABLE IF NOT EXISTS drill_case (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES drill_run(id),
    config       TEXT NOT NULL,   -- A=裸 B=+防禦提示 C=+淨化 D=兩者
    model        TEXT NOT NULL,
    material_id  INTEGER NOT NULL REFERENCES material(id),
    injection_id INTEGER REFERENCES injection(id),  -- NULL 表示對照組
    repeat_index INTEGER NOT NULL DEFAULT 0,  -- 同一組合的第幾次重複（LLM 有隨機性，單次不可採信）
    raw_output   TEXT,
    ranking      TEXT,   -- JSON 陣列，模型輸出的排序
    note         TEXT,   -- 模型自述的理由
    parse_ok     INTEGER,-- 輸出是否成功解析為 JSON
    disclosed    INTEGER,-- 模型是否主動指出素材含可疑指令（揭露率指標）
    carried      INTEGER,-- 多步流程專用：注入內容是否被中間步驟帶進下一步
    -- 規則判定，不靠 LLM judge：注入指向的錯誤結果是否成真
    manipulated  INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    usd          REAL,
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_case_run ON drill_case(run_id);
CREATE INDEX IF NOT EXISTS idx_run_app ON drill_run(application_id);

-- 稽核留痕：每一次分析留一筆可舉證的紀錄。
--
-- 刻意不存履歷全文，只存 SHA-256 指紋。HR 之後被申訴或勞檢時，
-- 拿當初那份檔案重算 hash 就能證明「這份檔案當時被判定為 X」，
-- 而系統不因為稽核需求額外囤積個資——這兩件事可以同時成立。
CREATE TABLE IF NOT EXISTS audit_record (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source_name    TEXT,             -- 檔名；直接貼上的內容記為「貼上的內容」
    content_sha256 TEXT NOT NULL,    -- 內容指紋，可比對同一份，但無法還原內容
    content_chars  INTEGER,
    model          TEXT NOT NULL,    -- 組織實際使用的那個模型
    verdict        TEXT NOT NULL,
    hidden_count   INTEGER,
    hidden_kinds   TEXT,             -- JSON 陣列：規則偵測到的隱藏通道類型
    finding_kinds  TEXT,             -- JSON 陣列：語意偵測到的注入類型
    ranking_bare   TEXT,             -- JSON：無防護那側的排序
    ranking_safe   TEXT,             -- JSON：有防護那側的排序
    rank_shifts    TEXT,             -- JSON：名次變化
    impact_line    TEXT,
    usd            REAL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_record(created_at);
