"""should_extract_memory — skip trivial noise, never skip real facts."""

import pytest

from bella.memory_extract import should_extract_memory

REPLY = "Sure, I've noted that."  # a plausible assistant reply, long enough for the floor


@pytest.mark.parametrize("msg", [
    "hi", "hey", "thanks", "thank you", "ok", "okay", "cool", "nice",
    "yes", "no", "yep", "nope", "lol", "haha", "bye", "goodnight",
    "got it", "sounds good", "makes sense", "/status", "sure",
])
def test_trivial_messages_skipped(msg):
    assert should_extract_memory(msg, REPLY) is False


@pytest.mark.parametrize("msg", [
    "My professor prefers PDF submissions.",
    "I'm using Python 3.11 for this project.",
    "Remember that I prefer concise responses.",
    "We deploy on Fridays and the staging box is called octopus.",
    "I live in Berlin so use CET for reminders.",
    "note that the API base url changed to api.v2.example.com",
])
def test_real_facts_pass(msg):
    assert should_extract_memory(msg, REPLY) is True


def test_short_but_factual_passes():
    assert should_extract_memory("i use nvim", "Noted — nvim it is, good choice for staying on the keyboard.") is True


def test_empty_and_tiny_skipped():
    assert should_extract_memory("", REPLY) is False
    assert should_extract_memory("k", "ok") is False
