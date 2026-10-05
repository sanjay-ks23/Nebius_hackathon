"""Patch edit matching, the SEARCH/REPLACE format, and the feedback a rejected patch gives back to the model."""
import pytest

from stopproof import agent, patching

FILES = {"worker.py": "class Worker:\n    def handle(self, job):\n        body = job['body']\n        send(body)\n"}


def test_stray_newlines_around_an_exact_match_are_tolerated():
    diff = patching.edits_to_diff(FILES, [{"path": "worker.py", "old": "\n        send(body)\n", "new": "\n        guard()\n        send(body)\n"}])
    assert "+        guard()" in diff and "-        send(body)" not in diff


def test_a_near_miss_is_rejected_with_the_closest_real_line():
    with pytest.raises(patching.Rejected, match=r"closest line in the file is '    def handle\(self, job\):'"):
        patching.edits_to_diff(FILES, [{"path": "worker.py", "old": "    def handle(self,job):", "new": "x"}])


@pytest.mark.parametrize("old", ["", "        not in the file", "        "])
def test_edits_must_match_exactly_once(old):
    with pytest.raises(patching.Rejected, match="exactly once"):
        patching.edits_to_diff(FILES, [{"path": "worker.py", "old": old, "new": "x"}])


def test_a_patch_that_breaks_syntax_is_rejected_and_names_the_offending_line():
    diff = patching.edits_to_diff(FILES, [{"path": "worker.py", "old": "        send(body)", "new": "        send(body)}], "}])
    with pytest.raises(patching.Rejected) as exc:
        patching.apply_diff(diff, FILES, {"worker.py"}, set())
    assert "does not parse" in str(exc.value) and "send(body)" in str(exc.value) and "stray JSON punctuation" in str(exc.value)


def test_a_valid_edit_round_trips_through_diff_and_apply():
    diff = patching.edits_to_diff(FILES, [{"path": "worker.py", "old": "        send(body)", "new": "        if body:\n            send(body)"}])
    patched = patching.apply_diff(diff, FILES, {"worker.py"}, set())
    assert patched["worker.py"].endswith("        if body:\n            send(body)\n")


def test_edit_blocks_round_trip_and_ignore_commentary():
    edits = [
        {"path": "worker.py", "old": "        send(body)\n", "new": "        if body:\n            send(body)\n"},
        {"path": "stop.py", "old": "x = 1\n", "new": ""},
    ]
    reply = "Here is the fix:\n```\n" + agent.format_edit_blocks(edits) + "\n```\nHope that helps."
    assert agent.parse_edit_blocks(reply) == edits


def test_a_reply_without_blocks_is_rejected_not_guessed():
    with pytest.raises(patching.Rejected, match="no SEARCH/REPLACE blocks"):
        agent.parse_edit_blocks('{"edits": [{"path": "worker.py", "old": "a", "new": "b"}]}')


def test_a_truncated_block_is_dropped_rather_than_half_applied():
    complete = agent.format_edit_blocks([{"path": "a.py", "old": "x = 1\n", "new": "x = 2\n"}])
    reply = complete + "\n<<<<<<< SEARCH b.py\ny = 1\n=======\ny = "
    assert [e["path"] for e in agent.parse_edit_blocks(reply)] == ["a.py"]
