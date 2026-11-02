"""端到端冒烟:无外部依赖验证全流水线(假数据源 + mock LLM)。

抓取→入库→分析评分→聚类→报告→模板→生成→文章落库
运行: .venv/Scripts/python -m app.pipeline_demo
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from app.analyzer.service import analyze
from app.collectors.base import BaseCollector, BaseHotTopic
from app.collectors.storage import save_hot_topics
from app.core.checkpoint import RunRecorder, new_run_id
from app.core.db import connect, init_db, query
from app.generator.llm import LLMClient
from app.generator.service import generate_article
from app.generator.templates import seed_default_template


class DemoCollector(BaseCollector):
    """假数据源:模拟抓取到的两条 AI 热点。"""
    source = "demo"

    def fetch(self, mode: str = "auto"):
        return [
            BaseHotTopic(
                source="wechat", title="阿里发布新一代大模型,参数规模创纪录",
                url="https://demo/1", platform_hot=98000.0, author="AI前线",
                summary="某厂发布新一代大模型,在多项基准测试中刷新纪录",
                fetch_mode="crawler",
            ),
            BaseHotTopic(
                source="douyin", title="国产AI芯片大爆发,算力成本持续下降",
                url="https://demo/2", platform_hot=52000000.0, author="科技君",
                summary="多家国产芯片厂商推出新品,算力成本大幅下降",
                fetch_mode="crawler",
            ),
        ]


class DemoLLM(LLMClient):
    """假 LLM:返回符合模板的 JSON(模拟真实生成,长度达标)。"""
    def __init__(self):
        pass

    def generate(self, system_prompt, user_prompt, **kw):
        title = user_prompt.splitlines()[0].replace("【写作主题】", "").strip()
        return json.dumps({
            "title": f"{title[:18]}刷屏背后",
            "lead": f"这两天,{title[:12]}引发广泛讨论。作为普通读者,这件事意味着什么?本文带你读懂。" * 3,
            "body": ("据公开报道,该事件的核心事实如下:相关企业公布技术进展,"
                     "行业分析认为这将影响产业链格局。本文从事件回顾、影响分析、"
                     "发展趋势三个角度展开解读。" * 45)[:1300],
            "quote": "有业内人士评价:这是一次重要的技术里程碑。",
            "cta": "关注本号,第一时间获取科技圈深度解读。觉得有用,欢迎点赞转发。" * 2,
        }, ensure_ascii=False)


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    init_db(tmp)
    run_id = new_run_id()

    print("=== ① 抓取(假数据源) ===")
    collector = DemoCollector(keywords=["大模型", "AI", "芯片"], top_n=10)
    raw = collector.collect()
    print(f"    抓到 {len(raw)} 条热点")
    saved = save_hot_topics(raw, str(tmp))
    print(f"    入库 {saved} 条(合规元数据)")

    with RunRecorder(run_id, str(tmp)) as run:
        run.log("fetch", "success", f"入库 {saved} 条")

        print("=== ② 分析归纳(清洗→评分→聚类→报告) ===")
        conn = connect(tmp)
        topics = query(conn, "SELECT * FROM hot_topics")
        conn.close()
        result = analyze(topics, {"normalize_method": "percentile",
                                  "source_weight": {"wechat": 0.6, "douyin": 0.4},
                                  "keywords": ["大模型", "AI", "芯片", "算力"],
                                  "decay_half_life_hours": 24, "top_n": 20})
        for t in result["scored"]:
            print(f"    [{t['source']}] {t['title'][:24]}... heat={t['heat_score']} rank_pct={t['rank_pct']}")
        print(f"    聚类主题: {[c['topic'] for c in result['clusters']]}")
        print(f"    Top1 选题: {result['report']['topics'][0]['topic']}")
        run.log("analyze", "success", f"聚出 {len(result['clusters'])} 个主题")

        print("=== ③ 文章生成(mock LLM + 真实模板) ===")
        seed_default_template(str(tmp))
        top_topic = result["scored"][0]
        art = generate_article(
            top_topic, "tpl_ai_news_v1", "s1", run_id,
            llm=DemoLLM(), db_path=str(tmp),
        )
        print(f"    生成文章 #{art['id']}: {art['title'][:30]}... (ok={art['ok']})")
        run.log("generate", "success", f"文章 #{art['id']}")

        print("=== ④ 断点回溯 ===")
        conn = connect(tmp)
        logs = query(conn, "SELECT stage, status FROM run_logs WHERE run_id=?", (run_id,))
        arts = query(conn, "SELECT id, title, status, body FROM articles")
        conn.close()
        for lg in logs:
            print(f"    [{lg['stage']}] {lg['status']}")
        a = arts[0]
        print(f"    正文长度: {len(a['body'])} 字 → 状态 {a['status']}")

    print("\n全流水线演示通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
