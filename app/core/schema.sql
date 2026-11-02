-- 公众号文章自动化 Agent 系统 Schema
-- 对齐 PRD V3.0 第 7 章数据模型

-- 热点表(热点池 + 知识库留存;合规约束:仅元数据,无正文全文)
CREATE TABLE IF NOT EXISTS hot_topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,              -- wechat / douyin
  fetch_mode TEXT,                   -- api / crawler
  title TEXT NOT NULL,
  url TEXT,
  platform_hot REAL,
  author TEXT,
  summary TEXT,                      -- 摘要(非全文)
  category TEXT,
  pub_at TEXT,                       -- 原文发布时间(可选,供时效衰减;抓取时间用 created_at)
  heat_score REAL,                   -- 综合热度(归一化后)
  rank_pct REAL,                     -- 单平台归一化值
  analyzed_at TEXT,                  -- 最近一次被分析(评分)的时刻
  first_analyzed_at TEXT,            -- **首次**被分析的时刻,只写一次、永不覆盖 —— 「今日分析」统计用
                                     -- (analyzed_at 每次分析都会被刷新,拿它按天统计 = 把历史全算进今天)
  fetch_date TEXT NOT NULL,
  expired_at TEXT,                   -- 合规存储期限(默认抓取日+30天)
  deleted INTEGER DEFAULT 0,         -- 一键删稿软删除
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 稿件表
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id INTEGER REFERENCES hot_topics(id),
  template_id TEXT NOT NULL,
  structure_id TEXT,                 -- 选用的文章结构方案
  title TEXT NOT NULL,
  lead TEXT,
  thesis TEXT,                       -- 核心论点(论点驱动结构专用):明确、可被反驳的判断
  body TEXT,
  quote TEXT,
  cta TEXT,
  status TEXT DEFAULT 'generated',   -- generated/pushed/pending_review/approved/edited/published/backfilled
  draft_url TEXT,
  published_url TEXT,                -- 发表后的**公开链接**(mp.weixin.qq.com/s/xxx),数据面板用
  mp_msgid TEXT,                     -- 公众号后台的发表记录 msgid(与草稿 appmsgid 不同命名空间)
  mp_mass_sent INTEGER,              -- 是否群发过:1=群发(已通知) / 0=只发表(未通知) / NULL=未知
  reject_reason TEXT,
  fact_notes TEXT,                   -- 【待核实】标记记录
  fail_count INTEGER DEFAULT 0,      -- 失败次数(熔断计数)
  published_at TEXT,                 -- 人工发布确认时间
  source_refs TEXT,                  -- 生成参考溯源 JSON:主选题/聚类成员/分析报告/模板/喂给 LLM 的素材
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 发布效果回流表
CREATE TABLE IF NOT EXISTS article_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL REFERENCES articles(id),
  read_count INTEGER,                -- 阅读人数   (后台 appmsg-view / read_num)
  like_count INTEGER,                -- 点赞人数   (后台 appmsg-like / old_like_num ⚠️不是 like_num)
  share_count INTEGER,               -- 分享人数   (后台 appmsg-share / share_num)
  recommend_count INTEGER,           -- 推荐人数   (后台 appmsg-haokan / like_num;原「在看」)
  comment_count INTEGER,             -- 留言条数   (后台 appmsg-comment / comment_num)
  underline_count INTEGER,           -- 划线人数   (后台 appmsg-underline;需另抓 mpunderline,可能为空)
  favorite_count INTEGER,            -- ⚠️ 遗留列,语义不明且未使用,保留只为不破坏老库
  stat_date TEXT NOT NULL,           -- 统计日期
  fetched_at TEXT DEFAULT (datetime('now', 'localtime')),
  UNIQUE(article_id, stat_date)
);

-- 模板表(人工设计入库)
CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  definition TEXT NOT NULL,          -- JSON(含 structures 数组)
  enabled INTEGER DEFAULT 1,
  updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 配置表(控制面板,含评分参数/归一化方式/合规/回流/告警配置)
CREATE TABLE IF NOT EXISTS configs (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,               -- JSON
  updated_at TEXT DEFAULT (datetime('now', 'localtime')),
  updated_by TEXT
);

-- 定时任务表(人工设计)
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cron TEXT NOT NULL,
  actions TEXT NOT NULL,             -- 要执行的环节
  enabled INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 断点/运行日志表
CREATE TABLE IF NOT EXISTS run_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,              -- 一次完整流程的 ID
  stage TEXT NOT NULL,               -- fetch/analyze/generate/push/review/backfill
  status TEXT NOT NULL,              -- success/failed/checkpoint
  detail TEXT,
  started_at TEXT,
  finished_at TEXT
);

-- 知识库清理日志表(含合规期限清理/一键删稿记录)
CREATE TABLE IF NOT EXISTS kb_clean_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id INTEGER,
  clean_type TEXT,                   -- retention/dup/outdated/low_value/manual
  reason TEXT,
  cleaned_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_hot_fetch_date ON hot_topics(fetch_date);
CREATE INDEX IF NOT EXISTS idx_hot_source ON hot_topics(source);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic_id);
CREATE INDEX IF NOT EXISTS idx_runlogs_runid ON run_logs(run_id);
