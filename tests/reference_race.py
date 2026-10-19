from agent import Agent
from jobqueue import JobQueue
from stop import request_stop
from worker import Worker

from stopproof.runtime.races import Scenario, main


def build(workdir):
    queue = JobQueue(workdir / "queue.json")
    agent = Agent("agent-1", queue)
    worker = Worker(queue)
    return Scenario(
        start_agent=lambda: agent.run("Send Alice an email: the meeting is cancelled"),
        stop=lambda: request_stop(agent),
        drain_worker=worker.run_until_empty,
    )


if __name__ == "__main__":
    main(build)
