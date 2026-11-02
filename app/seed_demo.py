"""演示数据填充:清理失败记录 + 补充热点 + 用 mock LLM 真实生成 2 篇 →
推进到待审核状态。运行:`.venv/Scripts/python -m app.seed_demo`
"""
from __future__ import annotations

import json
import sys

from app.analyzer.service import analyze
from app.core.checkpoint import new_run_id
from app.core.config import get_all_config
from app.core.db import connect, execute, query
from app.generator.llm import LLMClient
from app.generator.service import generate_article
from app.generator.templates import seed_default_template


class DemoLLM(LLMClient):
    def __init__(self):
        pass

    def generate(self, system_prompt, user_prompt, **kw):
        title = user_prompt.splitlines()[0].replace("【写作主题】", "").strip()
        body_core = (
            "据公开报道,事件核心事实如下:相关企业于近期公布了技术进展,业界分析认为这将重塑产业链格局。"
            "本文从事件回顾、影响分析、未来展望三个角度展开解读。\n\n"
            "一、事件回顾:综合多方公开信息看,此次发布涉及模型架构与训练范式的多项升级。"
            "据厂商披露,新版本在推理效率、上下文长度、多模态能力等关键指标上均有显著提升。"
            "值得指出的是,这些数字均来自官方披露,具体效果仍需第三方独立测评验证。\n\n"
            "二、为什么值得关注:对普通开发者而言,更低的调用价格与更稳的响应延迟,意味着日常工具类应用的成本可控。"
            "对企业用户,这次更新带来了新的私有化部署选项,合规与数据本地化顾虑有所缓解。"
            "对整个行业,头部厂商持续迭代会进一步压缩中小团队的同质化空间,倒逼差异化竞争。\n\n"
            "三、我们的观点:技术迭代从来不是孤立事件。每一次基础模型的能力跃迁,都会在上层应用、"
            "开发者工具、内容生产链路上引发连锁反应。短期看,关注头部厂商的 API 定价策略与开发者生态补贴;"
            "长期看,真正能拉开差距的,仍然是面向具体场景的工程化能力与数据飞轮。\n\n"
            "【信息来源】相关报道与厂商官方公告"
        )
        # *3 拉到约 1300 字,落入模板区间 960~2160
        body = (body_core + "\n") * 3
        return json.dumps({
            "title": f"{title[:18]}刷屏背后:3 个值得关注的信号",
            "lead": f"这两天,{title[:12]}在科技圈刷屏。作为普通读者,这件事到底意味着什么?本文用 3 分钟带你读懂。" * 2,
            "body": body,
            "quote": "有资深从业者评价:这不是一次简单的版本号更新,而是行业进入新阶段的标志。",
            "cta": "关注本号,第一时间获取科技圈深度解读。觉得有用,欢迎点赞转发。" * 2,
        }, ensure_ascii=False)


def main() -> int:
    seed_default_template()

    # 1. 清理历史失败/旧稿,保留待审核
    conn = connect()
    conn.execute("DELETE FROM articles WHERE status='generation_failed'")
    conn.commit()
    print("[清] 已清除历史生成失败记录")

    # 2. 补充热点(覆盖多主题)
    new_topics = [
        ("wechat", "crawler", "Anthropic 发布 Claude 4,代码能力大幅提升", 158000, "https://demo/anthropic-claude4"),
        ("douyin", "crawler", "国产 GPU 集群算力突破,大模型训练成本骤降", 92000000, "https://demo/gpu-cluster"),
        ("wechat", "crawler", "字节豆包多模态升级,实时视频理解上线", 72000, "https://demo/doubao-mm"),
        ("wechat", "crawler", "马斯克 xAI 推出 Grok 4,主打实时搜索", 210000, "https://demo/grok4"),
        ("douyin", "crawler", "AI 编程助手 Cursor 估值突破百亿美元", 48000000, "https://demo/cursor"),
    ]
    for src, mode, title, hot, url in new_topics:
        conn.execute(
            "INSERT INTO hot_topics(source, fetch_mode, title, url, platform_hot, fetch_date) VALUES(?,?,?,?,?,?)",
            (src, mode, title, url, float(hot), "2026-09-09"),
        )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM hot_topics WHERE deleted=0").fetchone()["c"]
    print(f"[抓] 热点总数:{total} (新插入 {len(new_topics)} 条)")

    # 3. 跑 analyze
    cfg = get_all_config()
    rows = query(conn, "SELECT * FROM hot_topics WHERE deleted=0")
    conn.close()
    result = analyze(rows, cfg)
    print(f"[析] 聚出 {len(result['clusters'])} 主题:")
    for c in result["clusters"][:3]:
        print(f"    - {c['topic']}: {c['count']} 条,热度 {c['heat_sum']:.2f}")

    # 4. 用 mock LLM 生成 2 篇(选热度最高 2 条)
    rid = new_run_id()
    scored = sorted(result["scored"], key=lambda t: -float(t.get("heat_score") or 0))[:2]
    generated = []
    for topic in scored:
        # 注意 generate_article 内部要查 topic.id,这里需要 db 里有对应 id
        topic_id = next((r["id"] for r in rows if r["title"] == topic["title"]), None)
        topic_with_id = {**topic, "id": topic_id}
        try:
            art = generate_article(topic_with_id, "tpl_ai_news_v1", "s1", rid,
                                   llm=DemoLLM())
            generated.append(art)
            print(f"[生] 文章 #{art['id']}: {art['title']} (ok)")
        except Exception as e:
            print(f"[生] 失败 topic={topic.get('title')}: {e}")

    # 5. 推进到 pending_review(模拟"已推送,等人工审")
    conn = connect()
    for art in generated:
        conn.execute(
            "UPDATE articles SET status='pending_review', draft_url='https://mp.weixin.qq.com/cGI-bin/draft' WHERE id=?",
            (art["id"],),
        )
    conn.commit()
    print(f"[推] {len(generated)} 篇文章已置为 pending_review(等待人工审核)")

    # 6. 让看板有"已发布"和"回流数据":模拟一条已发布 + 回流
    if generated:
        first = generated[0]
        conn.execute(
            "UPDATE articles SET status='published', published_at=datetime('now','localtime','-2 day'), reject_reason=NULL WHERE id=?",
            (first["id"],),
        )
        conn.execute(
            """INSERT INTO article_stats(article_id, read_count, like_count, share_count, favorite_count, stat_date)
               VALUES(?,?,?,?,?,?)""",
            (first["id"], 1820, 95, 56, 38, "2026-09-09"),
        )
        print(f"[回] 文章 #{first['id']} 标记为已发布(2 天前)+ 写入回流数据")

    conn.commit()
    # 最终统计
    final = {
        "fetch": conn.execute("SELECT COUNT(*) AS c FROM hot_topics WHERE deleted=0").fetchone()["c"],
        "articles": conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"],
        "review": conn.execute("SELECT COUNT(*) AS c FROM articles WHERE status='pending_review'").fetchone()["c"],
        "published": conn.execute("SELECT COUNT(*) AS c FROM articles WHERE status='published'").fetchone()["c"],
        "stats": conn.execute("SELECT COUNT(*) AS c FROM article_stats").fetchone()["c"],
        "failed": conn.execute("SELECT COUNT(*) AS c FROM articles WHERE status='generation_failed'").fetchone()["c"],
    }
    conn.close()
    print(f"\n[终] 热点:{final['fetch']} · 文章:{final['articles']} · 待审核:{final['review']} · 已发布:{final['published']} · 回流:{final['stats']} · 失败:{final['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())