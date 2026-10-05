"""Run the agent, then let the worker drain the queue."""
import sys

from agent import Agent
from jobqueue import JobQueue
from worker import Worker

if __name__ == "__main__":
    queue = JobQueue("queue.json")
    Agent("agent-1", queue).run(sys.argv[1] if len(sys.argv) > 1 else "Send Alice an email: the meeting is cancelled")
    Worker(queue).run_until_empty()
