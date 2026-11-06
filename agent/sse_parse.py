# -*- coding: utf-8 -*-
"""agent.sse_parse — 【完成】标记流式抽取（v1.0.654 A4 单源化）
=================================================================
原实现：team.py 与 auto_loop.py 各持一份 95% 相同的「滚动缓冲 + rsplit」token 累积块，
study_engine.py 另持一份无滚动缓冲的简化版（「【完成】」跨 chunk 分裂时会漏，
v1.0.271 在 /api/chat 修过的问题）。三处收敛为本类；chat/scheduler 的单行
end-of-stream 抽取语义不同（split 首次出现/全文兜底），保留各自实现。
"""


class FinalAnswerTracker:
    """从 token 流抽取「【完成】」标记之后的最终答复。

    滚动缓冲：标记跨 chunk 分裂（「【完」+「成】」两段）时单 chunk `in` 检测会漏——
    保留尾部 24 字符与下一段拼接后再检测；标记出现前的文本不进答复，
    标记后的文本（含后续 chunk）持续追加。"""

    def __init__(self, mark="【完成】", tail=24):
        self.mark = mark
        self.tail_len = tail
        self.answer = ""
        self._tail = ""

    def feed(self, text):
        t = self._tail + (text or "")
        if self.mark in t:
            self.answer += t.rsplit(self.mark, 1)[1]
            self._tail = ""
        else:
            self._tail = t[-self.tail_len:]
            if self.answer:
                self.answer += t

    def result(self):
        return self.answer
