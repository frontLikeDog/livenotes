# -*- coding: utf-8 -*-
"""端到端：下播收尾与用户总结共用一次调用，用量落账，任务可查。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import llm, scheduler, settings, store, tasks

SID = "LIVE_TEST"


def _cfg() -> dict:
    return {"base": "http://example.invalid/v1", "key": "k", "model": "test-model",
            "temperature": 0.2, "timeout": 5, "configured": True, "source": "test"}


class SummaryFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "test.db")
        self.patches = [
            mock.patch.object(store, "DB_PATH", self.db),
            mock.patch.object(settings, "resolve", return_value=_cfg()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        conn = store.connect()
        try:
            store.init_db(conn)
            store.insert_session(conn, {
                "id": SID, "title": "测试场次", "started_at": "2026-11-05T10:00:00",
                "duration_s": 600, "source_mode": "mixed",
            })
            store.insert_utterances(conn, [
                {"session_id": SID, "t_start": i * 10.0, "t_end": i * 10.0 + 8,
                 "speaker_id": "SPK_0%d" % (i % 2), "speaker_label": "说话人%d" % (i % 2),
                 "text": "这是第%d句测试发言，内容足够长，用来触发大模型总结的阈值。" % i,
                 "category": "substantive", "value_tag": "normal"}
                for i in range(20)
            ])
        finally:
            conn.close()
        self.sched = scheduler.Scheduler(concurrency=1, backoff_base=0, backoff_max=0,
                                         persist=True, name="test")
        # 业务处理器注册在进程级单例上；换成测试调度器后要把它们带过来。
        for kind, handler in scheduler.SCHEDULER._handlers.items():
            self.sched.register(kind, handler)
        self.sched_patch = mock.patch.object(scheduler, "SCHEDULER", self.sched)
        self.sched_patch.start()
        self.addCleanup(self.sched_patch.stop)
        self.addCleanup(self.sched.shutdown)
        self.calls = 0

    def _fake_chat(self, cfg, payload, timeout, total_timeout=None):
        self.calls += 1
        body = {
            "overview": "这是一段测试总结。",
            "points": [{"title": "测试话题", "gist": "测试要点",
                        "evidence_ids": [1, 2]}],
            "title": "测试标题", "title_alts": ["备选标题"],
        }
        return {"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 23,
                          "total_tokens": 123}}

    def test_finalize_then_manual_summary_reuses_one_call(self):
        with mock.patch.object(llm, "_post_chat", side_effect=self._fake_chat):
            first = tasks.submit_finalize(SID)
            out1 = first.wait(timeout=10)
            self.assertEqual(first.status, "succeeded")
            self.assertIn("llm", str(out1.get("engine") or ""))

            second = tasks.submit_summary(SID, {
                "t_from": None, "t_to": None, "categories": [], "speakers": [], "q": "",
                "exclude_low_value": True, "exclude_overlap": False, "min_conf": 0.9,
            }, "content", "auto")
            out2 = second.wait(timeout=10)

        self.assertEqual(self.calls, 1, "同一份稿子只应调用一次模型")
        self.assertEqual(second.id, first.id)
        self.assertTrue(second.cached)
        self.assertEqual(out2["overview"], out1["overview"])
        self.assertEqual(store.llm_usage_total(session_id=SID), 123)
        jobs = self.sched.snapshot()["jobs"]
        self.assertTrue(any(j["id"] == first.id and j["status"] == "succeeded"
                            for j in jobs))

    def test_llm_failure_is_reported_and_not_cached(self):
        def boom(cfg, payload, timeout, total_timeout=None):
            self.calls += 1
            raise llm.LLMError("HTTP 401 bad key", status=401, retryable=False)

        with mock.patch.object(llm, "_post_chat", side_effect=boom):
            first = tasks.submit_finalize(SID)
            with self.assertRaises(scheduler.JobFailed) as ctx:
                first.wait(timeout=10)
        # 模型失败不再降级成本地稿：任务明确失败，库里也不能多出一份「总结」
        self.assertIn("本次没有生成总结", str(ctx.exception))
        self.assertEqual(first.status, "failed")
        self.assertEqual(self.calls, 1)
        conn = store.connect()
        try:
            self.assertIsNone(store.latest_summary(conn, SID, "content"))
            note = store.get_note(conn, SID) or {}
            self.assertEqual(note.get("state"), "failed")
        finally:
            conn.close()

        with mock.patch.object(llm, "_post_chat", side_effect=boom):
            second = tasks.submit_summary(SID, {
                "t_from": None, "t_to": None, "categories": [], "speakers": [], "q": "",
                "exclude_low_value": True, "exclude_overlap": True, "min_conf": None,
            }, "content", "auto")
            with self.assertRaises(scheduler.JobFailed):
                second.wait(timeout=10)
        self.assertNotEqual(second.id, first.id, "失败结果不该被当成成功缓存")
        self.assertFalse(second.cached)
        self.assertEqual(self.calls, 2, "用户再点一次应该真正重新调用模型")

    def test_summary_uses_long_timeout_not_the_default(self):
        """整场总结要比分类/润色这类短调用等得久。

        实测一场 1720 句的总结，同一份输入两次分别要 113 秒和 >180 秒（推理型
        模型的思考量不固定）。用默认的 180 秒仍会砍断长尾，所以总结单独传
        LLM_SUMMARY_TOTAL_TIMEOUT。
        """
        from app.config import LLM_SUMMARY_TOTAL_TIMEOUT, LLM_TOTAL_TIMEOUT

        self.assertGreater(LLM_SUMMARY_TOTAL_TIMEOUT, LLM_TOTAL_TIMEOUT,
                           "总结的上限必须比默认值宽松，否则等于没覆盖长尾")

        seen: list = []

        def spy(cfg, payload, timeout, total_timeout=None):
            seen.append(total_timeout)
            return self._fake_chat(cfg, payload, timeout, total_timeout)

        with mock.patch.object(llm, "_post_chat", side_effect=spy):
            job = tasks.submit_finalize(SID)
            job.wait(timeout=10)
            self.assertEqual(job.status, "succeeded")

        self.assertTrue(seen, "总结应当真的调用了模型")
        self.assertEqual(seen[0], LLM_SUMMARY_TOTAL_TIMEOUT,
                         "总结调用必须带长超时，否则长场次会被砍断")


if __name__ == "__main__":
    unittest.main()
