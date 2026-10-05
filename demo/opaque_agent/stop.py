"""STOP handling: cancels the agent and everything it already handed off."""


def request_stop(agent):
    agent.cancel()
    agent.queue.cancel_origin(agent.agent_id)
