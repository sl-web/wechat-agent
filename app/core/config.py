"""配置中心:configs 表读写 + 默认值兜底。

优先级:configs 表(控制面板可改) > 代码内置默认值。
敏感密钥(LLM Key 等)不放此表,直接从 .env 读取。
"""
from __future__ import annotations

import json
import sqlite3

from app.core.db import connect

# 默认配置(configs 表为空时使用)
DEFAULTS: dict = {
    # 数据来源:默认 RSS 聚合(免费稳定);wechat/douyin 保留(需第三方 API)
    "data_sources": ["rss"],
    # RSS 订阅源。⚠️ 实际**没有界面**能改它(2026-09-13 全文扫过前端,PanelView 里没有 rss_feeds)
    # —— 想增删源只能直接写 configs 表,或调 `POST /api/config/rss_feeds`。
    # 原文注释写的是「控制面板可增删」,那是一句假话,已改掉。
    "rss_feeds": [
        {"name": "量子位", "url": "https://www.qbitai.com/feed", "enabled": True},
        {"name": "IT之家", "url": "https://www.ithome.com/rss/", "enabled": True},
        {"name": "InfoQ", "url": "https://www.infoq.cn/feed", "enabled": True},
        {"name": "雷峰网", "url": "https://www.leiphone.com/feed", "enabled": True},
        {"name": "开源中国", "url": "https://www.oschina.net/news/rss", "enabled": True},
        {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "enabled": True},
        {"name": "少数派", "url": "https://sspai.com/feed", "enabled": True},
        {"name": "新浪科技", "url": "https://rss.sina.com.cn/tech/rollnews.xml", "enabled": True},
    ],
    # 抓取范围:领域关键词
    "keywords": ["大模型", "AI", "人工智能", "芯片", "算力", "自动驾驶",
                 "机器人", "智能硬件", "GPT", "Anthropic", "OpenAI",
                 "具身智能", "模型", "智驾", "半导体", "机器学习"],
    # 抓取量(单次最多入库条数;源变多后调大,避免截断丢失当日新内容)
    "top_n": 60,
    "per_keyword_limit": 50,
    # 频率(小时)
    "fetch_interval_hours": 24,
    # 评分参数
    "normalize_method": "percentile",      # percentile / log / minmax
    "source_weight": {"wechat": 0.6, "douyin": 0.4, "rss": 0.5},
    "decay_half_life_hours": 24,
    "relevance_enabled": True,
    # 传播潜力(把回流数据接回选题):从账号自己的转发数据学「什么样的标题会被转」。
    # ⚠️ 传播系数是**有界**的(默认 ±35%),弱统计不该盖过热度与时效。见 analyzer/growth.py
    "virality_enabled": True,
    "virality_weight": 0.35,
    "growth_profile": {},                  # 由「重建传播画像」写入:学到的形态/词 lift
    # 发布时段建议(读者活跃时段)。经验值,可按自己后台的"阅读时段"改。
    "publish_window": "07:00-09:00, 12:00-13:00, 20:00-22:00",
    # 熔断
    "retry_limit": 3,
    "hotspot_blacklist_threshold": 5,
    # 合规
    "retention_days": 30,
    "crawler_daily_limit": 500,
    # 回流
    "backfill_enabled": True,
    "backfill_days_after_publish": [1, 3, 7],
    # 告警
    "review_stale_hours": 24,
    "notify_channels": [],
    # 每日生成篇数
    "daily_articles": 2,

    # ---- 写作立场:决定文章有没有「观点」----
    # 独到观点不会从并列的事实里长出来,它来自「谁在说、关心什么、必须表态」。
    "writer_persona": "长期跟踪 AI 与半导体产业的观察者,做过硬件产品与供应链",
    "writer_stance": "对技术叙事保持怀疑:先问谁付钱、谁承担成本、谁在讲这个故事",
    "writer_lens": "优先看商业化落地与成本结构,而不是参数表和发布会",
    # 论证姿态:industry_judgment(行业判断)/ critical(批判质疑)/
    #            teardown(深度拆解)/ forecast(趋势预测)
    "article_type": "industry_judgment",
    # 反综述护栏:命中综述特征即判不合格并重新生成(见 validator.py)
    "anti_recap_guard": True,
    # 转发钩子:强制 quote 可被单独转发、cta 给出转发/关注理由+留言引导、
    # body 含一处可收藏的干货。公众号涨粉几乎全靠分享,关掉等于只写字不传播。
    "share_hooks_enabled": True,
    # 默认文章结构:s3 = 论点驱动式(有观点);s1/s2 为原有的回顾式/盘点式
    "default_structure": "s3",
    # 生成时用哪个模板(模板库里的 id)。在「控制面板 → 模板库」点「设为生成默认」改它。
    # ⚠️ 以前这个键只在 pipeline 里被 `cfg.get(...)` 读、面板上根本没有 ——
    #    于是用户新建的模板永远选不上,"模板保存了却没作用"有一半原因在这里。
    "default_template": "tpl_ai_news_v1",

    # ---- 公众号草稿推送(M4)----
    "mp_author": "",                 # 推送时「作者」栏内容(留空则不填该栏)
    "mp_cover_image": "",            # 默认封面图本地路径(留空则跳过封面,人工在后台补)
    # 封面自动生成:开启后按文章标题用模板渲染封面(零版权风险),优先于 mp_cover_image
    "mp_cover_enabled": False,
    "mp_cover_style": "dark",        # dark 科技深色 / light 简约浅色 / blocks 色块对比
    "mp_cover_tag": "AI · 产业观察",  # 封面上的栏目标签(留空则不显示)
    "mp_daily_draft_limit": 5,       # 单日草稿推送上限(风控保护)
    "mp_interval_min": 2,            # 两次推送最小间隔(秒)
    "mp_interval_max": 5,            # 两次推送最大间隔(秒)
    "mp_scan_wait_seconds": 600,     # 扫码等待时长(秒);扫码需要人工操作,给足 10 分钟
    "mp_session_max_age_days": 5,    # 会话超过该天数则提示重新扫码
    # ---- 正文排版(C 批)----
    # 排版主题预设:tech 科技蓝 / minimal 简约浅色 / vivid 高对比。
    # 每套预设定义 主题色 + 字号 + 行距 + 段距 + 圆角 + 默认标题样式。
    "mp_layout_theme": "tech",
    # 小节标题样式:留空=跟随主题预设;bar 左色块 / badge 编号徽章 / center 居中带线
    "mp_heading_style": "",
    # 只覆盖主题色(留空=跟随主题预设)。浅色底由它混白自动推导,改一个值整套配色一起变
    "mp_accent_color": "",
}


def _load_defaults() -> dict:
    return json.loads(json.dumps(DEFAULTS))


def get_config(key: str, db: sqlite3.Connection | None = None) -> object:
    """读单个配置:表里有则用表的,否则用默认值。"""
    own = db is None
    conn = db or connect()
    try:
        row = conn.execute(
            "SELECT value FROM configs WHERE key=?", (key,)
        ).fetchone()
        if row:
            return json.loads(row["value"])
        return _load_defaults().get(key)
    finally:
        if own:
            conn.close()


def get_all_config(db: sqlite3.Connection | None = None) -> dict:
    """读全部配置(表值覆盖默认值)。"""
    own = db is None
    conn = db or connect()
    try:
        merged = _load_defaults()
        rows = conn.execute("SELECT key, value FROM configs").fetchall()
        for r in rows:
            try:
                merged[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                continue
        return merged
    finally:
        if own:
            conn.close()


def set_config(key: str, value: object, updated_by: str = "panel", db: sqlite3.Connection | None = None) -> None:
    """写配置(UPSERT),记录版本时间。"""
    own = db is None
    conn = db or connect()
    try:
        conn.execute(
            """INSERT INTO configs(key, value, updated_at, updated_by) VALUES(?,?,datetime('now','localtime'),?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 updated_at=datetime('now','localtime'), updated_by=excluded.updated_by""",
            (key, json.dumps(value, ensure_ascii=False), updated_by),
        )
        conn.commit()
    finally:
        if own:
            conn.close()
