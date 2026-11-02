"""数据库连接与初始化。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.logger import get_logger
from app.core.settings import settings

logger = get_logger("db")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_db_path() -> Path:
    return settings.data_dir / "app.db"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """建立连接,开启行级访问与 WAL 提升并发。"""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path | None = None) -> None:
    """执行 schema.sql,幂等建库建表;对已有库做轻量列迁移。"""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # 轻量迁移:老库补齐新增列(ALTER TABLE 幂等,已存在则跳过)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(hot_topics)")]
        if "pub_at" not in cols:
            conn.execute("ALTER TABLE hot_topics ADD COLUMN pub_at TEXT")
        if "analyzed_at" not in cols:
            conn.execute("ALTER TABLE hot_topics ADD COLUMN analyzed_at TEXT")
        if "first_analyzed_at" not in cols:
            conn.execute("ALTER TABLE hot_topics ADD COLUMN first_analyzed_at TEXT")
            # 老库回填:无从得知"第一次被分析的准确时刻",只能用 fetch_date 近似
            # (热点抓回来紧接着就会分析,同一天)。
            # ⚠️ **绝不能用 analyzed_at 回填** —— 它每次分析都被刷新成当前时间,
            # 拿它回填等于把历史上所有条目都算成"今天首次分析",正是要修掉的那个问题。
            # 存日期串没问题:统计口径是 date(first_analyzed_at)。
            conn.execute(
                "UPDATE hot_topics SET first_analyzed_at = fetch_date "
                "WHERE first_analyzed_at IS NULL AND analyzed_at IS NOT NULL"
            )
        acols = [r["name"] for r in conn.execute("PRAGMA table_info(articles)")]
        if "source_refs" not in acols:
            conn.execute("ALTER TABLE articles ADD COLUMN source_refs TEXT")
        if "thesis" not in acols:
            conn.execute("ALTER TABLE articles ADD COLUMN thesis TEXT")
        if "published_url" not in acols:
            conn.execute("ALTER TABLE articles ADD COLUMN published_url TEXT")
        if "mp_msgid" not in acols:
            conn.execute("ALTER TABLE articles ADD COLUMN mp_msgid TEXT")
        if "mp_mass_sent" not in acols:
            # NULL = 未知(还没同步过);1 = 群发过;0 = 只发表未群发
            conn.execute("ALTER TABLE articles ADD COLUMN mp_mass_sent INTEGER")
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(article_stats)")]
        # 老库的 article_stats 只有 read/like/share/favorite 四列;
        # 真实回流要存后台「发表记录」给的全部指标,补齐缺的列。
        for col in ("recommend_count", "comment_count", "underline_count"):
            if col not in scols:
                conn.execute(f"ALTER TABLE article_stats ADD COLUMN {col} INTEGER")
        conn.commit()

        # 内置模板的违禁词表随代码走:老库里可能还是早期的单字表,与 validator
        # 实际使用的词组表不一致(prompt 让模型避开 A、validator 按 B 判罚)。
        # 延迟导入避免 db ↔ templates 循环依赖;失败不能影响启动。
        try:
            from app.generator.templates import sync_default_ban_words

            if sync_default_ban_words(db_path):
                logger.info("已同步内置模板的违禁词表(修正历史遗留的陈旧列表)")
        except Exception as e:  # noqa: BLE001
            logger.warning("同步模板违禁词表失败(不影响启动): %s", e)
    finally:
        conn.close()


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """查询辅助,返回 dict 列表。"""
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """执行辅助,返回 lastrowid。"""
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid
