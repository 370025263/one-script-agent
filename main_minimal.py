"""
main_minimal.py — 把你草稿里的结构"最小改动跑通"的版本。

我想说的：
- 保留了你的模块划分：CLI / Tool 类 / Tools 注册表 / ContextManager / Model / Agent。
- 只修语法和明显的逻辑 bug（role 字符串、self、终止条件、main 入口倒装等）。
- 工具只实现 Read + Grep 两个，够跑通 act loop 即可。
- 没加错误处理、没加 max_turns、没加权限层、没加并发——这些留给 improved 版本。
- 协议走 OpenAI chat.completions（baseurl+api_key 风格），方便接 DeepSeek/Kimi/Qwen 等兼容端点。

跑法：
    export OPENAI_API_KEY=sk-xxx
    # 可选：export OPENAI_BASE_URL=https://api.deepseek.com/v1
    python main_minimal.py
"""

import json
import os
import subprocess
from openai import OpenAI


class CLI:
    def render_assistant(self, content, tool_calls):
        if content:
            print(f"\n[assistant] {content}")
        if tool_calls:
            for tc in tool_calls:
                print(f"  -> call {tc.function.name}({tc.function.arguments})")

    def render_tool(self, content, tool_id):
        preview = content if len(content) < 300 else content[:300] + "...<truncated>"
        print(f"  [tool_result {tool_id[-6:]}] {preview}")


class ReadTool:
    name = "read_file"
    description = "Read a UTF-8 text file. Returns content with 1-based line numbers."
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 2000},
        },
        "required": ["path"],
    }

    def __call__(self, path, offset=0, limit=2000):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        chunk = lines[offset : offset + limit]
        return "".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(chunk))


class GrepTool:
    name = "grep"
    description = "Search for a regex pattern in files under a path using ripgrep."
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
        },
        "required": ["pattern"],
    }

    def __call__(self, pattern, path="."):
        out = subprocess.run(
            ["rg", "-n", "--no-heading", pattern, path],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout or "(no matches)"


class Tools:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def call(self, name, args):
        return self._tools[name](**args)

    def schemas(self):
        return [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.schema}}
            for t in self._tools.values()
        ]


class ContextManager:
    def __init__(self, tools):
        self.history = []
        self.sys_p = self._assemble_sysp(tools)

    def _assemble_sysp(self, tools):
        tool_list = ", ".join(t.name for t in tools._tools.values())
        return f"You are a coding agent. cwd={os.getcwd()}. Tools: {tool_list}."

    def messages(self):
        return [{"role": "system", "content": self.sys_p}] + self.history

    def add_user(self, content):
        self.history.append({"role": "user", "content": content})

    def add_assistant(self, content, tool_calls):
        msg = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {
                    "name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ]
        self.history.append(msg)

    def add_tool(self, content, tool_id):
        self.history.append(
            {"role": "tool", "content": content, "tool_call_id": tool_id})


class Model:
    def __init__(self, model="gpt-4o-mini", temperature=0.2):
        self.client = OpenAI()  # reads OPENAI_API_KEY / OPENAI_BASE_URL from env
        self.model = model
        self.temperature = temperature

    def __call__(self, messages, tools_schema):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools_schema,
            temperature=self.temperature,
        )
        return resp.choices[0]


class Agent:
    def __init__(self, cli, tools, ctx, model):
        self.cli, self.tools, self.ctx, self.model = cli, tools, ctx, model

    def run(self):
        while True:
            try:
                user_input = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user_input:
                continue
            self.ctx.add_user(user_input)

            while True:
                choice = self.model(self.ctx.messages(), self.tools.schemas())
                msg = choice.message
                self.ctx.add_assistant(msg.content, msg.tool_calls)
                self.cli.render_assistant(msg.content, msg.tool_calls)

                if choice.finish_reason != "tool_calls":
                    break

                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result = str(self.tools.call(tc.function.name, args))
                    self.ctx.add_tool(result, tc.id)
                    self.cli.render_tool(result, tc.id)


def main():
    cli = CLI()
    tools = Tools()
    tools.register(ReadTool())
    tools.register(GrepTool())
    ctx = ContextManager(tools)
    model = Model()
    Agent(cli, tools, ctx, model).run()


if __name__ == "__main__":
    main()
