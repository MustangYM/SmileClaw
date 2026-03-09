from tools.shell import run as run_shell
from tools.shell import SCHEMA as SHELL_SCHEMA


TOOLS = {
    "shell": run_shell
}


SCHEMAS = [
    SHELL_SCHEMA
]


def execute_tool(name, args):

    if name not in TOOLS:
        return "Tool not found"

    tool = TOOLS[name]

    return tool(**args)