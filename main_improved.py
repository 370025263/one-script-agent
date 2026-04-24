"""
main_improved.py — 把上一轮批判的 8 个思路问题落地的版本。

我想说的：
每一块代码上面我都标了对应的批判点编号（#1~#8），方便对照。

#1 失败面：模型调用失败 → 带退避重试；工具抛异常 → 转成 is_error=True 的
   tool_result 继续喂给模型，而不是让 agent 崩。
#2 终止条件：max_turns 熔断、token 预算压缩、KeyboardInterrupt 只中断当前
   act loop 不杀 session。
#3 Tool schema：每个工具自带 schema；Tools.schemas() 是唯一真相源，system
   prompt 和 API 调用都从这里取。
#4 并发：工具类声明 parallel_safe；调度器把可并行的同批丢 ThreadPool，不可
   并行的串行跑，保持 tool_call_id 顺序。
#5 权限层：所有副作用工具经过 Approver.check()；策略：allow / deny /
   ask-once / always。这是安全边界不是装饰。
#6 CLI 解耦：Agent 只发事件（AgentEvent），CLI 是一个 listener；测试时换成
   silent listener 就行。
#7 Context 预算：tool_result 按"距今 N 轮之外"的规则降级为文件路径+head，
   原文持久化到 /tmp/agent_cache/，可通过 read_file 回读。
#8 tool_use_id 配对：assistant_with_tool_calls 后面必须紧跟等量 tool_result，
   用 _flush_tool_results 批量写入；中间任何异常都兜底成 is_error 的 result，
   保证配对不断。

仍然保持 OpenAI chat.completions 协议。没做的：streaming、memory 持久化、
真正的 token 计数（用字符数近似）——这些是下一层的事。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI, APIError, RateLimitError, APIConnectionError


# ---------------------------------------------------------------------------
# Events (#6) — Agent 只发事件，不知道下游是 CLI 还是别的
# ---------------------------------------------------------------------------
@dataclass
class AgentEvent:
    kind: str            # assistant_text | tool_call | tool_result | turn_end | error
    payload: dict


# ANSI 颜色助手；非 TTY 或 NO_COLOR 环境变量存在时自动降级为纯文本。
class _C:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    CYAN = "\x1b[36m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    BLUE = "\x1b[34m"
    GRAY = "\x1b[90m"

    ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def paint(cls, code: str, text: str) -> str:
        return f"{code}{text}{cls.RESET}" if cls.ENABLED else text


def _format_tool_args(args: dict) -> str:
    if not args:
        return "()"
    parts = []
    for k, v in args.items():
        if isinstance(v, str):
            s = v.replace("\n", "\\n")
            if len(s) > 48:
                s = s[:48] + "…"
            s = f"'{s}'"
        else:
            s = json.dumps(v, ensure_ascii=False)
            if len(s) > 48:
                s = s[:48] + "…"
        parts.append(f"{_C.paint(_C.DIM, k)}={s}")
    return "(" + ", ".join(parts) + ")"


class CLIListener:
    MAX_PREVIEW_CHARS = 600
    MAX_PREVIEW_LINES = 12

    def __call__(self, ev: AgentEvent):
        handler = getattr(self, f"_on_{ev.kind}", None)
        if handler:
            handler(ev.payload)

    def _on_assistant_text(self, p):
        text = (p.get("text") or "").strip()
        if not text:
            return
        label = _C.paint(_C.BOLD + _C.CYAN, "assistant")
        bar = _C.paint(_C.CYAN, "│")
        body = "\n".join(f"{bar} {line}" for line in text.splitlines())
        print(f"\n{label}\n{body}")

    def _on_tool_call(self, p):
        arrow = _C.paint(_C.GRAY, "→")
        name = _C.paint(_C.BOLD + _C.BLUE, p["name"])
        print(f"  {arrow} {name}{_format_tool_args(p['args'])}")

    def _on_tool_result(self, p):
        is_err = p.get("is_error")
        tag = _C.paint(_C.BOLD + _C.RED, "[err]") if is_err else _C.paint(_C.GREEN, "[ok] ")
        tid = _C.paint(_C.GRAY, p["tool_id"][-6:])
        content = p["content"]
        preview, truncated = self._clip(content)
        lines = preview.splitlines() or [""]
        if len(lines) == 1 and len(preview) <= 80 and not truncated:
            print(f"    {tag} {tid}  {lines[0]}")
            return
        size_hint = _C.paint(_C.GRAY, f"  ({len(content)}B)")
        print(f"    {tag} {tid}{size_hint}")
        bar = _C.paint(_C.GRAY, "│")
        for ln in lines:
            print(f"      {bar} {ln}")
        if truncated:
            print(f"      {_C.paint(_C.GRAY, '… (truncated)')}")

    def _on_error(self, p):
        tag = _C.paint(_C.BOLD + _C.RED, "error")
        print(f"\n{tag} {p['message']}")

    def _clip(self, content: str):
        lines = content.splitlines()
        truncated = False
        if len(lines) > self.MAX_PREVIEW_LINES:
            lines = lines[: self.MAX_PREVIEW_LINES]
            truncated = True
        clipped = "\n".join(lines)
        if len(clipped) > self.MAX_PREVIEW_CHARS:
            clipped = clipped[: self.MAX_PREVIEW_CHARS]
            truncated = True
        return clipped, truncated


# ---------------------------------------------------------------------------
# Tools (#3, #4) — 每个工具自带 schema 和 parallel_safe
# ---------------------------------------------------------------------------
@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    parallel_safe: bool
    side_effect: bool                # used by approver (#5)
    func: Callable[..., str]


class ReadTool:
    mtime_store: dict[str, float] = {}  # shared with EditTool for #3-bonus

    spec = ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file. Returns content with 1-based line numbers.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
        },
        parallel_safe=True,
        side_effect=False,
        func=None,  # set below
    )

    def __call__(self, path, offset=0, limit=2000):
        p = Path(path)
        ReadTool.mtime_store[str(p.resolve())] = p.stat().st_mtime
        with p.open(encoding="utf-8") as f:
            lines = f.readlines()
        chunk = lines[offset : offset + limit]
        return "".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))


ReadTool.spec.func = ReadTool()


class GrepTool:
    spec = ToolSpec(
        name="grep",
        description="Regex search under a path via ripgrep.",
        schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
            },
            "required": ["pattern"],
        },
        parallel_safe=True,
        side_effect=False,
        func=None,
    )

    def __call__(self, pattern, path="."):
        out = subprocess.run(
            ["rg", "-n", "--no-heading", pattern, path],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout or "(no matches)"


GrepTool.spec.func = GrepTool()


class ListTool:
    spec = ToolSpec(
        name="ls",
        description="List entries of a directory (non-recursive). "
                    "Prefix 'd ' for dirs, 'f ' for files.",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
            },
            "required": [],
        },
        parallel_safe=True,
        side_effect=False,
        func=None,
    )

    def __call__(self, path="."):
        p = Path(path)
        if not p.exists():
            raise RuntimeError(f"path not found: {path}")
        if not p.is_dir():
            raise RuntimeError(f"not a directory: {path}")
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        return "\n".join(
            f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries
        ) or "(empty)"


ListTool.spec.func = ListTool()


class BashTool:
    """执行 shell 命令；有副作用，默认走 approver。"""
    spec = ToolSpec(
        name="bash",
        description="Run a shell command. Returns stdout, stderr, and exit code.",
        schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
        parallel_safe=False,      # 命令之间可能有副作用依赖，串行更安全
        side_effect=True,         # 需权限审批 (#5)
        func=None,
    )

    def __call__(self, command, timeout=120):
        out = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        parts = []
        if out.stdout:
            parts.append(out.stdout.rstrip())
        if out.stderr:
            parts.append(f"[stderr]\n{out.stderr.rstrip()}")
        parts.append(f"[exit={out.returncode}]")
        return "\n".join(parts)


BashTool.spec.func = BashTool()


class EditTool:
    """read-before-edit + mtime 乐观锁；新文件用 old_string='' 触发创建。"""
    spec = ToolSpec(
        name="edit_file",
        description=(
            "Edit a file or create a new one. "
            "To create: pass old_string='' and the target path must not exist; "
            "new_string becomes the whole file content. "
            "To edit: old_string must appear exactly once and the file must have "
            "been read in this session since last modified."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        parallel_safe=False,         # 同文件并发会错乱
        side_effect=True,            # 需要权限审批 (#5)
        func=None,
    )

    def __call__(self, path, old_string, new_string):
        p = Path(path).resolve()
        # 新文件创建分支：old_string 必须为空，且路径不存在
        if not p.exists():
            if old_string != "":
                raise RuntimeError(
                    f"file not found: {path}; to create a new file pass old_string=''"
                )
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_string, encoding="utf-8")
            ReadTool.mtime_store[str(p)] = p.stat().st_mtime
            return f"created {path}"

        # 已存在 → 走原本的 read-before-edit 语义
        seen_mtime = ReadTool.mtime_store.get(str(p))
        if seen_mtime is None:
            raise RuntimeError("must read_file before edit_file")
        if p.stat().st_mtime != seen_mtime:
            raise RuntimeError("file changed on disk since last read; re-read and retry")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count != 1:
            raise RuntimeError(f"old_string matched {count} times; need exactly 1")
        p.write_text(text.replace(old_string, new_string), encoding="utf-8")
        ReadTool.mtime_store[str(p)] = p.stat().st_mtime
        return f"edited {path}"


EditTool.spec.func = EditTool()


class Tools:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        self._specs[spec.name] = spec

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def schemas(self):
        return [
            {"type": "function", "function": {
                "name": s.name, "description": s.description, "parameters": s.schema}}
            for s in self._specs.values()
        ]


# ---------------------------------------------------------------------------
# #5 Approver — 权限层
# ---------------------------------------------------------------------------
class Approver:
    def __init__(self):
        self.always: set[str] = set()

    def check(self, tool_name: str, args: dict) -> bool:
        if tool_name in self.always:
            return True
        q = _C.paint(_C.BOLD + _C.YELLOW, "?")
        name = _C.paint(_C.BOLD + _C.BLUE, tool_name)
        args_s = _C.paint(_C.DIM, json.dumps(args, ensure_ascii=False))
        hint = _C.paint(_C.GRAY, "[y/N/a=always]")
        ans = input(f"  {q} allow {name}({args_s}) {hint} ").strip().lower()
        if ans == "a":
            self.always.add(tool_name)
            return True
        return ans == "y"


# ---------------------------------------------------------------------------
# #7 ContextManager — token 预算 + tool_result 降级
# ---------------------------------------------------------------------------
CACHE_DIR = Path("/tmp/agent_cache")
CACHE_DIR.mkdir(exist_ok=True)


class ContextManager:
    MAX_CHARS = 200_000                # 约 66K tokens (3 chars/token)，deepseek-v3.2 128K 上下文下约 50% 给 history
    KEEP_RECENT_TOOLS = 8              # 最近 N 个 tool_result 保留原文

    def __init__(self, tools: Tools):
        self.history: list[dict] = []
        self.sys_p = self._assemble_sysp(tools)

    def _assemble_sysp(self, tools):
        tool_list = ", ".join(tools._specs)
        return (
            f"You are a coding agent. cwd={os.getcwd()}. Tools: {tool_list}. "
            f"Old tool results may be replaced by a file path summary; use read_file to recover."
        )

    def messages(self):
        self._maybe_compress()
        return [{"role": "system", "content": self.sys_p}] + self.history

    def add_user(self, content):
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content, tool_calls, reasoning_content=None):
        msg = {"role": "assistant", "content": content or ""}
        # DeepSeek thinking mode: reasoning_content 必须原样带回，否则下一轮报 400
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {
                    "name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ]
        self.history.append(msg)

    def add_tool_result(self, content: str, tool_id: str, is_error: bool):
        prefix = "ERROR: " if is_error else ""
        self.history.append({
            "role": "tool", "content": prefix + content, "tool_call_id": tool_id,
        })

    def _maybe_compress(self):
        total = sum(len(m.get("content", "") or "") for m in self.history)
        if total < self.MAX_CHARS:
            return
        # 保留最近 N 个 tool 消息的原文，更旧的降级为 head + 文件路径
        tool_indices = [i for i, m in enumerate(self.history) if m["role"] == "tool"]
        for i in tool_indices[: -self.KEEP_RECENT_TOOLS]:
            msg = self.history[i]
            raw = msg["content"]
            if raw.startswith("[cached:"):
                continue
            h = hashlib.sha1(raw.encode()).hexdigest()[:12]
            path = CACHE_DIR / f"{h}.txt"
            path.write_text(raw, encoding="utf-8")
            head = "\n".join(raw.splitlines()[:20])
            msg["content"] = f"[cached:{path}] head:\n{head}\n...(use read_file to recover)"


# ---------------------------------------------------------------------------
# #1 Model — 带退避重试
# ---------------------------------------------------------------------------
class Model:
    def __init__(self, model="deepseek-v4-flash", temperature=0.2, max_retries=5):
        self.client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries

    def __call__(self, messages, tools_schema):
        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    tools=tools_schema, temperature=self.temperature,
                )
                return resp.choices[0]
            except (RateLimitError, APIConnectionError, APIError) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise last_err


# ---------------------------------------------------------------------------
# Agent (#1, #2, #4, #8)
# ---------------------------------------------------------------------------
class Agent:
    MAX_TURNS = 60

    def __init__(self, tools: Tools, ctx: ContextManager, model: Model,
                 approver: Approver, listener: Callable[[AgentEvent], None]):
        self.tools, self.ctx, self.model, self.approver, self.emit = (
            tools, ctx, model, approver, listener)
        self.pool = ThreadPoolExecutor(max_workers=8)

    def run(self):
        banner = _C.paint(_C.BOLD + _C.CYAN, "agent_learn")
        tip = _C.paint(_C.GRAY, "Ctrl-C to cancel current turn · Ctrl-D to exit")
        print(f"\n{banner}  {tip}")
        prompt = _C.paint(_C.BOLD + _C.CYAN, ">")
        while True:
            try:
                user_input = input(f"\n{prompt} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user_input:
                continue
            self.ctx.add_user(user_input)
            try:
                self._act_loop()
            except KeyboardInterrupt:
                self.emit(AgentEvent("error", {"message": "act loop interrupted"}))
                # #8: 万一中途被打断，留下的 assistant_with_tool_calls 需要伪造
                # tool_result 补齐配对，否则下一轮 API 报 400
                self._pair_dangling_tool_calls()

    def _act_loop(self):
        for turn in range(self.MAX_TURNS):
            try:
                choice = self.model(self.ctx.messages(), self.tools.schemas())
            except Exception as e:
                self.emit(AgentEvent("error", {"message": f"model failed: {e}"}))
                return
            msg = choice.message
            self.ctx.add_assistant(msg.content, msg.tool_calls)
            self.emit(AgentEvent("assistant_text", {"text": msg.content}))

            if choice.finish_reason != "tool_calls":
                return

            self._run_tool_batch(msg.tool_calls)
        self.emit(AgentEvent("error", {"message": f"max_turns={self.MAX_TURNS} exceeded"}))

    # #4 并发调度 + #5 审批 + #8 结果配对
    def _run_tool_batch(self, tool_calls):
        pending = []
        for tc in tool_calls:
            spec = self.tools.spec(tc.function.name)
            args = self._parse_args(tc.function.arguments)
            self.emit(AgentEvent("tool_call", {"name": spec.name, "args": args}))

            if spec.side_effect and not self.approver.check(spec.name, args):
                self._record_result(tc.id, "denied by user", is_error=True)
                continue
            pending.append((tc, spec, args))

        # 可并行的先并发跑，不可并行的串行兜底
        parallel = [x for x in pending if x[1].parallel_safe]
        serial = [x for x in pending if not x[1].parallel_safe]

        futures = {
            tc.id: self.pool.submit(self._safe_invoke, spec, args)
            for tc, spec, args in parallel
        }
        for tc, spec, args in pending:  # 按原顺序收集，维护 tool_use_id 次序
            if tc.id in futures:
                content, is_error = futures[tc.id].result()
            else:
                content, is_error = self._safe_invoke(spec, args)
            self._record_result(tc.id, content, is_error)

    def _safe_invoke(self, spec: ToolSpec, args: dict):
        try:
            return str(spec.func(**args)), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True   # #1

    def _record_result(self, tool_id, content, is_error):
        self.ctx.add_tool_result(content, tool_id, is_error)
        self.emit(AgentEvent(
            "tool_result", {"tool_id": tool_id, "content": content, "is_error": is_error}))

    def _parse_args(self, raw: str) -> dict:
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _pair_dangling_tool_calls(self):
        if not self.ctx.history:
            return
        last = self.ctx.history[-1]
        if last["role"] != "assistant" or "tool_calls" not in last:
            return
        answered = {m.get("tool_call_id") for m in self.ctx.history if m["role"] == "tool"}
        for tc in last["tool_calls"]:
            if tc["id"] not in answered:
                self.ctx.add_tool_result("interrupted", tc["id"], is_error=True)


def main():
    tools = Tools()
    tools.register(ReadTool.spec)
    tools.register(ListTool.spec)
    tools.register(GrepTool.spec)
    tools.register(BashTool.spec)
    tools.register(EditTool.spec)
    ctx = ContextManager(tools)
    model = Model()
    approver = Approver()
    listener = CLIListener()
    Agent(tools, ctx, model, approver, listener).run()


if __name__ == "__main__":
    main()
