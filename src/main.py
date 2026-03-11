import sys
import termios
import tty

from memory.memory import Memory
from agent.agent import Agent


def _read_key():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = sys.stdin.read(1)
        if first == "\x1b":
            second = sys.stdin.read(1)
            third = sys.stdin.read(1)
            return first + second + third
        return first
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _approval_menu():
    options = [
        ("allow_once", "同意一次"),
        ("allow_always", "始终同意"),
        ("deny", "拒绝")
    ]
    selected = 0
    line_count = len(options)

    print("审批操作（方向键选择，回车确认）：")

    def draw(first_draw=False):
        if not first_draw:
            sys.stdout.write(f"\x1b[{line_count}F")
        for idx, (_, text) in enumerate(options):
            marker = ">" if idx == selected else " "
            sys.stdout.write("\x1b[2K")
            sys.stdout.write(f"{marker} {idx + 1}、{text}\n")
        sys.stdout.flush()

    draw(first_draw=True)

    while True:
        key = _read_key()
        if key == "\x1b[A":
            selected = (selected - 1) % line_count
            draw()
            continue
        if key == "\x1b[B":
            selected = (selected + 1) % line_count
            draw()
            continue
        if key in ("\n", "\r"):
            value, text = options[selected]
            print(f"已选择：{text}")
            return value


memory = Memory()
agent = Agent(memory)

while True:

    user_input = input("You: ")

    memory.add("user", user_input)

    agent.run()

    while agent.has_pending_approval():
        decision = _approval_menu()
        agent.resolve_approval(decision)
        agent.run()

    agent.finalize_run_if_idle()
