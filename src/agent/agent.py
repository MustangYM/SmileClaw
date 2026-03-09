import json

from llm.llm import ask_llm
from tools.registry import execute_tool


class Agent:

    def __init__(self, memory):
        self.memory = memory

    def run(self):

        while True:

            messages = self.memory.get()
            print("准备调用LLM Messages:", messages)

            reply = ask_llm(messages)

            print("LLM:", reply)

            try:

                data = json.loads(reply)

                if "tool" in data:

                    tool_name = data["tool"]
                    args = data["args"]

                    result = execute_tool(tool_name, args)

                    print("Tool result:", result)

                    self.memory.add("assistant", reply)
                    self.memory.add("assistant", f"Tool result: {result}")

                    continue

            except json.JSONDecodeError:
                pass

            self.memory.add("assistant", reply)

            break