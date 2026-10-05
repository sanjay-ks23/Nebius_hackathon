"""STOP handling: cancels the agent that initiated the work."""


def request_stop(agent):
    agent.cancel()
