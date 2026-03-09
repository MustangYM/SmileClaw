from memory.memory import Memory
from agent.agent import Agent


memory = Memory()
agent = Agent(memory)

while True:

    user_input = input("You: ")

    memory.add("user", user_input)

    agent.run()