"""main_improved.py 流式 SSE 解析单测（纯 mock，不联网）。

运行：python3 -m unittest test_stream -v
"""
import io
import json
import os
import unittest
from unittest import mock

# Model.__init__ 会读环境变量，给个占位避免 None
os.environ.setdefault("OPENAI_API_KEY", "test")

from main_improved import Model


def sse(obj) -> bytes:
    """构造一行 SSE：data: {json}\n\n"""
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def content_event(text):
    return sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})


def reasoning_event(text):
    return sse({"choices": [{"delta": {"content": None, "reasoning_content": text},
                             "finish_reason": None}]})


def chunk_bytes(payload: bytes, size: int):
    """把一段完整字节流按 size 切成块——模拟 HTTP chunk 任意边界。"""
    return [payload[i:i + size] for i in range(0, len(payload), size)]


class ParseSSETest(unittest.TestCase):
    def setUp(self):
        self.m = Model()

    def _parse(self, byte_chunks, capture_tokens=False):
        # 只收集正文 token，保持原有断言不变
        tokens = []
        cb = (lambda t, kind: tokens.append(t) if kind == "content" else None) \
            if capture_tokens else None
        result = self.m._parse_sse(iter(byte_chunks), on_token=cb)
        return result, tokens

    def test_basic_content(self):
        chunks = [content_event("Hello"), content_event(" world"),
                  b"data: [DONE]\n\n"]
        result, tokens = self._parse(chunks, capture_tokens=True)
        self.assertEqual(result.message.content, "Hello world")
        self.assertEqual(tokens, ["Hello", " world"])
        self.assertIsNone(result.message.tool_calls)

    def test_multiple_events_in_one_chunk(self):
        # 关键回归：多个 SSE 事件挤在同一个网络块里，必须全部处理（之前的 bug 只处理最后一条）
        merged = content_event("一") + content_event("二") + content_event("三")
        result, tokens = self._parse([merged, b"data: [DONE]\n\n"], capture_tokens=True)
        self.assertEqual(result.message.content, "一二三")
        self.assertEqual(tokens, ["一", "二", "三"])

    def test_chinese_split_across_chunks(self):
        # 关键回归：中文 3 字节序列被 chunk 边界切断，不能乱码
        payload = content_event("你好世界，这是一段中文测试") + b"data: [DONE]\n\n"
        for size in (1, 2, 3, 5, 7, 13):
            with self.subTest(chunk_size=size):
                result, _ = self._parse(chunk_bytes(payload, size))
                self.assertEqual(result.message.content, "你好世界，这是一段中文测试")

    def test_emoji_split_across_chunks(self):
        # 4 字节 UTF-8（emoji）被切断
        payload = content_event("ok👍🚀done") + b"data: [DONE]\n\n"
        for size in (1, 2, 3, 4, 6):
            with self.subTest(chunk_size=size):
                result, _ = self._parse(chunk_bytes(payload, size))
                self.assertEqual(result.message.content, "ok👍🚀done")

    def test_reasoning_model_separates_content(self):
        # deepseek-v4-flash 推理模型：reasoning_content 是思维链，content 才是答案
        chunks = [
            reasoning_event("让我想想"), reasoning_event("用户要打招呼"),
            content_event("你好"),
            b"data: [DONE]\n\n",
        ]
        kinds = []
        result = self.m._parse_sse(iter(chunks), on_token=lambda t, k: kinds.append((k, t)))
        # 正文只含答案，不含思维链
        self.assertEqual(result.message.content, "你好")
        self.assertEqual(result.message.reasoning_content, "让我想想用户要打招呼")
        # 回调按顺序区分 reasoning / content
        self.assertEqual(kinds, [("reasoning", "让我想想"),
                                 ("reasoning", "用户要打招呼"),
                                 ("content", "你好")])

    def test_reasoning_split_across_chunks(self):
        payload = reasoning_event("思考中文不能乱码") + content_event("答案") + b"data: [DONE]\n\n"
        for size in (1, 2, 3, 5):
            with self.subTest(chunk_size=size):
                result = self.m._parse_sse(iter(chunk_bytes(payload, size)))
                self.assertEqual(result.message.reasoning_content, "思考中文不能乱码")
                self.assertEqual(result.message.content, "答案")

    def test_tool_calls_fragmented(self):
        # tool_call 的 name/arguments 分多个 delta 到达，需累积拼接
        events = [
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "read_file", "arguments": ""}}]},
                "finish_reason": None}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"path"'}}]},
                "finish_reason": None}]}),
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ': "a.py"}'}}]},
                "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ]
        result, _ = self._parse(events)
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(len(result.message.tool_calls), 1)
        tc = result.message.tool_calls[0]
        self.assertEqual(tc.id, "call_1")
        self.assertEqual(tc.function.name, "read_file")
        self.assertEqual(json.loads(tc.function.arguments), {"path": "a.py"})

    def test_parallel_tool_calls(self):
        events = [
            sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c0", "function": {"name": "ls", "arguments": "{}"}},
                {"index": 1, "id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
            ]}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ]
        result, _ = self._parse(events)
        ids = [tc.id for tc in result.message.tool_calls]
        self.assertEqual(ids, ["c0", "c1"])  # 按 index 排序

    def test_ignores_non_data_and_blank_lines(self):
        chunks = [b": keep-alive\n\n", b"\n", content_event("x"), b"data: [DONE]\n\n"]
        result, _ = self._parse(chunks)
        self.assertEqual(result.message.content, "x")

    def test_stops_at_done_ignores_trailing(self):
        chunks = [content_event("a"), b"data: [DONE]\n\n", content_event("SHOULD_NOT_APPEAR")]
        result, _ = self._parse(chunks)
        self.assertEqual(result.message.content, "a")

    def test_no_trailing_done(self):
        # 有些端点流结束不发 [DONE]，靠迭代器耗尽收尾
        chunks = [content_event("a"), content_event("b")]
        result, _ = self._parse(chunks)
        self.assertEqual(result.message.content, "ab")


class CrossEndpointTest(unittest.TestCase):
    """不同 OpenAI 兼容端点的 SSE 方言：OpenAI / vLLM / 代理注入等。"""

    def setUp(self):
        self.m = Model()

    def _parse(self, chunks):
        return self.m._parse_sse(iter(chunks))

    def test_openai_style_no_reasoning(self):
        # 标准 OpenAI：无 reasoning_content，带 [DONE]
        chunks = [content_event("Hello"), content_event(" GPT"), b"data: [DONE]\n\n"]
        res = self._parse(chunks)
        self.assertEqual(res.message.content, "Hello GPT")
        self.assertIsNone(res.message.reasoning_content)

    def test_vllm_no_done_sentinel(self):
        # 部分 vLLM 部署流结束不发 [DONE]，靠迭代器耗尽收尾
        chunks = [content_event("from"), content_event(" vllm")]
        res = self._parse(chunks)
        self.assertEqual(res.message.content, "from vllm")

    def test_usage_only_final_chunk(self):
        # OpenAI stream_options.include_usage：最后一块 choices=[]，仅含 usage
        chunks = [content_event("hi"),
                  sse({"choices": [], "usage": {"total_tokens": 5}}),
                  b"data: [DONE]\n\n"]
        res = self._parse(chunks)
        self.assertEqual(res.message.content, "hi")

    def test_data_without_space(self):
        # 有的端点输出 data:{...} 不带空格
        line = b"data:" + json.dumps(
            {"choices": [{"delta": {"content": "x"}}]}).encode() + b"\n\n"
        res = self._parse([line, b"data:[DONE]\n\n"])
        self.assertEqual(res.message.content, "x")

    def test_tool_call_without_index(self):
        # 个别端点单工具调用不带 index 字段
        ev = sse({"choices": [{"delta": {"tool_calls": [
            {"id": "c0", "function": {"name": "ls", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]})
        res = self._parse([ev, b"data: [DONE]\n\n"])
        self.assertEqual(len(res.message.tool_calls), 1)
        self.assertEqual(res.message.tool_calls[0].function.name, "ls")

    def test_keepalive_and_malformed_lines_skipped(self):
        # SSE 注释行(:)、空行、坏 JSON 都跳过，不中断流
        chunks = [b": keepalive\n\n", b"\n",
                  b"data: not-json-garbage\n\n",
                  content_event("ok"), b"data: [DONE]\n\n"]
        res = self._parse(chunks)
        self.assertEqual(res.message.content, "ok")

    def test_mid_stream_error_raises(self):
        chunks = [content_event("partial"),
                  sse({"error": {"message": "rate limited", "code": 429}})]
        with self.assertRaises(RuntimeError):
            self._parse(chunks)


class FakeResp:
    """模拟 urlopen 返回的响应对象，支持 read(n) 和上下文管理。"""
    def __init__(self, data):
        self._b = io.BytesIO(data)

    def read(self, n):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ModelCallE2ETest(unittest.TestCase):
    """mock urlopen，跑通 Model.__call__ 整条链路。"""

    def test_full_request_response(self):
        os.environ["OPENAI_MODEL"] = "my-model"
        payload = (content_event("你好") + content_event("，世界")
                   + sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                   + b"data: [DONE]\n\n")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["auth"] = req.headers.get("Authorization")
            return FakeResp(payload)

        m = Model()
        toks = []
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            res = m(messages=[{"role": "user", "content": "hi"}],
                    tools_schema=[],
                    on_token=lambda t, kind: toks.append(t) if kind == "content" else None)

        self.assertEqual(m.model, "my-model")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(captured["body"]["model"], "my-model")
        self.assertEqual(captured["auth"], "Bearer test")
        self.assertEqual(res.message.content, "你好，世界")
        self.assertEqual(res.finish_reason, "stop")
        self.assertEqual("".join(toks), "你好，世界")

    def test_retries_on_5xx(self):
        # 500 应触发重试；这里第二次成功
        os.environ.pop("OPENAI_MODEL", None)
        import urllib.error
        payload = content_event("ok") + b"data: [DONE]\n\n"
        calls = {"n": 0}

        def flaky_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b""))
            return FakeResp(payload)

        m = Model(max_retries=3)
        m.max_retries = 3
        with mock.patch("urllib.request.urlopen", flaky_urlopen), \
             mock.patch("time.sleep", lambda s: None):
            res = m(messages=[], tools_schema=[])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res.message.content, "ok")

    def test_4xx_raises_immediately(self):
        import urllib.error
        calls = {"n": 0}

        def bad_urlopen(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {}, io.BytesIO(b'{"error":"nope"}'))

        m = Model(max_retries=3)
        with mock.patch("urllib.request.urlopen", bad_urlopen), \
             mock.patch("time.sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                m(messages=[], tools_schema=[])
        self.assertEqual(calls["n"], 1)  # 4xx 不重试


if __name__ == "__main__":
    unittest.main()
