from openai import OpenAI
from dotenv import load_dotenv
import os
from tools.registry import SCHEMAS

load_dotenv()

SYSTEM_PROMPT = f"""
You are an AI agent.

You can use the following tools:

{SCHEMAS}

If a tool is needed, return JSON:

{{
 "tool": "tool_name",
 "args": {{ ... }}
}}

Otherwise reply normally.
"""

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)


def ask_llm(messages):

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT}
        ] + messages
    )

    return response.choices[0].message.content
