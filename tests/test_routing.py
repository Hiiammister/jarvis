"""
Router classification tests — the safety-critical part of the latency work.

Focus: false positives. A conversational message must never trigger a tool; a
question ABOUT a command must never run that command.
"""

import pytest

from bella.router import Route, classify, tools_for_groups


def route(text, **kw):
    return classify(text, **kw).route


# ── Tier 0: greetings / acknowledgements ──────────────────────────────────

@pytest.mark.parametrize("text", [
    "hi", "hey", "hello", "yo", "  Hello!  ", "hey bella", "good morning",
    "good evening", "howdy",
])
def test_greetings_are_local(text):
    d = classify(text)
    assert d.route is Route.LOCAL_RESPONSE
    assert d.response and len(d.response) < 40


@pytest.mark.parametrize("text", [
    "thanks", "thank you", "thanks so much", "ty", "cheers", "ok", "okay",
    "got it", "cool", "nice", "perfect", "makes sense",
])
def test_acknowledgements_are_local(text):
    assert route(text) is Route.LOCAL_RESPONSE


@pytest.mark.parametrize("text", ["bye", "goodbye", "see you", "later", "good night", "lol", "haha"])
def test_farewells_and_filler_are_local(text):
    assert route(text) is Route.LOCAL_RESPONSE


def test_ack_after_question_falls_through():
    # "ok" right after Bella asked something might be answering it.
    assert route("ok", last_was_question=True) is not Route.LOCAL_RESPONSE
    assert route("yes", last_was_question=True) is not Route.LOCAL_RESPONSE
    # a greeting is still safe though
    assert route("hey", last_was_question=True) is Route.LOCAL_RESPONSE


# ── Tier 0: slash commands never hit the model ────────────────────────────

@pytest.mark.parametrize("text", [
    "/help", "/status", "/doctor", "/tools", "/memory", "/history",
    "/recall foo", "/clear", "/listen", "/silent", "/tts", "/exit", "/benchmark",
])
def test_slash_commands(text):
    assert route(text) is Route.SLASH_COMMAND


def test_slash_doctor_is_not_a_shell_command():
    d = classify("/doctor")
    assert d.route is Route.SLASH_COMMAND
    assert d.tool_name == "/doctor"
    assert d.route is not Route.DIRECT_TOOL


# ── DIRECT_TOOL: media / volume / reminders ───────────────────────────────

@pytest.mark.parametrize("text,tool,action", [
    ("pause", "spotify", "pause"),
    ("pause music", "spotify", "pause"),
    ("resume", "spotify", "play"),
    ("next", "spotify", "next"),
    ("skip", "spotify", "next"),
    ("previous", "spotify", "previous"),
    ("stop the music", "spotify", "pause"),
    ("what's playing", "spotify", "current"),
])
def test_spotify_transport_direct(text, tool, action):
    d = classify(text)
    assert d.route is Route.DIRECT_TOOL
    assert d.tool_name == tool
    assert d.tool_args["action"] == action


@pytest.mark.parametrize("text,expect", [
    ("volume up", {"action": "adjust", "delta": 10}),
    ("volume down", {"action": "adjust", "delta": -10}),
    ("mute", {"action": "mute"}),
    ("unmute", {"action": "unmute"}),
    ("set volume to 30", {"action": "set", "level": 30}),
    ("increase volume by 20%", {"action": "adjust", "percent": 20}),
])
def test_volume_direct(text, expect):
    d = classify(text)
    assert d.route is Route.DIRECT_TOOL
    assert d.tool_name == "volume"
    assert d.tool_args == expect


def test_list_reminders_direct():
    d = classify("list my reminders")
    assert d.route is Route.DIRECT_TOOL
    assert (d.tool_name, d.tool_args) == ("reminder", {"action": "list"})


def test_simple_reminder_creation_direct():
    d = classify("remind me to call mom at 5pm")
    assert d.route is Route.DIRECT_TOOL
    assert d.tool_name == "reminder"
    assert d.tool_args["action"] == "add"
    assert d.tool_args["text"] == "call mom"
    assert "due_date" in d.tool_args


def test_git_status_direct():
    for text in ["git status", "run git status", "what's my git status", "show me git status"]:
        d = classify(text)
        assert d.route is Route.DIRECT_TOOL, text
        assert d.tool_name == "shell"
        assert d.tool_args["command"] == "git status"


# ── FALSE POSITIVES — the important cases ─────────────────────────────────

def test_lyric_pause_does_not_trigger_spotify():
    d = classify("The song says pause for a moment")
    assert d.route is not Route.DIRECT_TOOL
    assert d.tool_name != "spotify"


def test_question_about_git_status_does_not_run_it():
    for text in [
        "Can you explain what git status means?",
        "what does git status do",
        "how does git status work",
        "explain git status",
    ]:
        d = classify(text)
        assert d.route is not Route.DIRECT_TOOL, text
        assert d.route is Route.FAST_CHAT, text  # knowledge question, no tools


def test_reminded_of_something_does_not_create_reminder():
    d = classify("I was reminded of something yesterday")
    assert d.route is not Route.DIRECT_TOOL
    assert d.tool_name != "reminder"


def test_talking_about_volume_is_not_a_command():
    d = classify("the volume of that book is huge")
    assert d.route is not Route.DIRECT_TOOL


# ── FAST_CHAT vs FULL_AGENT ──────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "how are you?",
    "tell me a joke",
    "what does recursion mean?",
    "explain the difference between a list and a tuple",
    "who are you",
    "define idempotent",
    "what's a monad",
])
def test_conversational_is_fast_chat(text):
    d = classify(text)
    assert d.route is Route.FAST_CHAT, (text, d.reason)


@pytest.mark.parametrize("text", [
    "search for the latest FastAPI release notes",
    "what's the weather today",
    "debug why this docker container keeps restarting",
    "inspect this repository and find why the tests fail",
    "push this to github",
    "open github.com",
    "run the test suite",
    "read config.py and summarise it",
    "create a file called notes.md",
])
def test_tool_requests_are_full_agent(text):
    d = classify(text)
    assert d.route is Route.FULL_AGENT, (text, d.reason)


def test_low_confidence_falls_back_to_full_toolset():
    # a vague imperative with a tool word but no clear group
    d = classify("go check on that thing for me")
    assert d.route in (Route.FULL_AGENT, Route.FAST_CHAT)


# ── tool-group selection ────────────────────────────────────────────────

def test_tool_groups_media():
    d = classify("play something upbeat on spotify and turn it up")
    assert d.route is Route.FULL_AGENT
    assert "media" in d.tool_groups
    assert set(tools_for_groups(d.tool_groups)) >= {"spotify", "volume"}


def test_tool_groups_coding():
    d = classify("fix the failing test in the auth module")
    assert "coding" in d.tool_groups
    assert "shell" in tools_for_groups(d.tool_groups)


def test_tool_groups_web():
    d = classify("search for python 3.13 release date")
    assert "web" in d.tool_groups


def test_tools_for_groups_empty_means_all_via_registry():
    # empty groups -> caller uses full set; helper returns []
    assert tools_for_groups([]) == []


# ── generation profiles ────────────────────────────────────────────────

def test_profiles_assigned():
    assert classify("hi").profile == "FAST"
    assert classify("how are you").profile == "FAST"
    assert classify("what does recursion mean").profile in ("FAST", "NORMAL")
    assert classify("debug why the build fails").profile == "DEEP"
    assert classify("write a detailed comparison of REST and GraphQL").profile == "DEEP"
