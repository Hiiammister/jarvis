"""
bella.router — fast-path intent detection.

    user input
        |
    fast-path router
      |-- match  -> execute the tool directly, format a short reply, skip the LLM
      +-- no match -> fall through to the agent loop

Why it exists: the 8B model has a documented history of *fabricating* action
success for bare media/volume/reminder commands ("Playback is now paused",
"Volume set to 40") without ever emitting a tool call, and of passing the wrong
argument shape ("by 20%" -> delta instead of percent). Whole-string matches on
unambiguous phrasing are executed deterministically instead. Anything the router
can't parse with confidence returns None and goes to the model — it only ever
adds reliability, never blocks a request.

This module is pure string -> (tool, arguments); it does not import the registry
or run anything itself. The runtime calls `match()` then `format_reply()`.

Moved verbatim from jarvis.py — behaviour and coverage are unchanged.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class RouterMatch:
    tool: str
    arguments: dict


# ── request classification ──────────────────────────────────────────────────
#
#     INPUT -> normalize -> classify() -> RouteDecision
#
#        LOCAL_RESPONSE   deterministic canned reply, no model
#        SLASH_COMMAND    handled by the interface, no model
#        DIRECT_TOOL      run one tool now, no model  (media / volume / reminders / git status)
#        FAST_CHAT        one lightweight model call, NO tools, small output cap
#        FULL_AGENT       the full tool-calling agent loop, scoped tool set
#
# classify() is pure and cheap: string ops, regexes, set lookups. It never
# calls a model. On any uncertainty it favours FULL_AGENT (correct but slower)
# over guessing.


class Route(str, enum.Enum):
    LOCAL_RESPONSE = "LOCAL_RESPONSE"
    SLASH_COMMAND = "SLASH_COMMAND"
    DIRECT_TOOL = "DIRECT_TOOL"
    FAST_CHAT = "FAST_CHAT"
    FULL_AGENT = "FULL_AGENT"


@dataclass
class RouteDecision:
    route: Route
    confidence: float = 0.0
    tool_name: str | None = None
    tool_args: dict | None = None
    response: str | None = None            # LOCAL_RESPONSE / SLASH_COMMAND
    reason: str = ""
    tool_groups: list[str] = field(default_factory=list)   # FULL_AGENT
    profile: str = "DEEP"                  # FAST | NORMAL | DEEP (generation cap)


# Tool capability groups — the router picks likely-relevant groups for
# FULL_AGENT so the model isn't handed every schema for every request.
TOOL_GROUPS: dict[str, list[str]] = {
    "media": ["spotify", "volume"],
    "web": ["web_search", "open_browser"],
    "memory": ["memory", "recall"],
    "coding": ["shell", "read_file", "write_file", "github"],
    "reminders": ["reminder", "todo"],
}


def tools_for_groups(groups: list[str]) -> list[str]:
    """Flatten group names -> tool names (deduped, order-stable). Empty groups
    means 'no opinion' -> caller should use the full set."""
    seen: list[str] = []
    for g in groups:
        for name in TOOL_GROUPS.get(g, []):
            if name not in seen:
                seen.append(name)
    return seen


# ── Spotify transport + system volume ────────────────────────────────────────

_SPOTIFY_FAST_RULES: list[tuple[str, str]] = [
    (r"(pause|hold|freeze)( it| this| that| everything|"
     r" (the )?(music|song|track|playback|audio)| spotify| playing)?", "pause"),
    (r"(stop|kill|end|halt)( it| this| that|"
     r" (the )?(music|song|track|playback|audio)| spotify| playing)?", "pause"),
    (r"(resume|unpause|continue|keep playing|carry on|play on|un-?pause)"
     r"( it| this| the (music|song|track|playback)| playing| spotify)?", "play"),
    (r"(play|start|unpause)( it| the music| music| the song| the track|"
     r" again| spotify| something)?", "play"),
    (r"(next|skip|forward)( it| this| song| track| the song| this song|"
     r" this track| ahead| forward| please)?", "next"),
    (r"(previous|prev|go back|back a( song| track| bit)?|last song|"
     r"previous song|previous track|rewind)( song| track| one| please)?", "previous"),
    (r"(what('?s| is| ?)( currently| now)? playing( right now| now| atm)?"
     r"|(what|which) (song|track|tune)( is|'?s)?"
     r" (this|that|playing|on|being played|currently playing|is being played)"
     r"( right now| now| atm)?"
     r"|(the )?current (song|track|tune)"
     r"|now playing"
     r"|what is this (song|track)"
     r"|what('?s| is) (this|the) (song|track)( called| named)?"
     r"|what am i listening to"
     r"|name of (this|the current|the) (song|track)"
     r"|song name"
     r"|who('?s| is)( this)? (playing|singing( this)?)"
     r"|who sings this)", "current"),
]

_VOL_OBJ = r"( it| the (volume|sound|audio|music))?"
_VOL_HEDGE = r"( a bit| a little| a touch| some| please)?"

_VOL_UP = (r"(?:increase|raise|boost|bump|crank|"
           r"turn\s+(?:it\s+|the\s+volume\s+|the\s+sound\s+)?up|turn\s+up)")
_VOL_DOWN = (r"(?:decrease|lower|reduce|drop|cut|"
             r"turn\s+(?:it\s+|the\s+volume\s+|the\s+sound\s+)?down|turn\s+down|"
             r"knock\s+(?:it\s+)?down|bring\s+(?:it\s+)?down)")
_VOL_BY = (r"(?:\s+it|\s+(?:the\s+)?volume|\s+(?:the\s+)?sound|\s+(?:the\s+)?audio)?"
           r"\s+by\s+(\d{1,3})\s*(%|percent|points?|pts?)?")


def _vol_by(match: "re.Match", sign: int) -> dict:
    n = int(match.group(1))
    unit = match.group(2) or ""
    if "%" in unit or "percent" in unit:
        return {"action": "adjust", "percent": sign * n}
    return {"action": "adjust", "delta": sign * n}


def _fast_volume_input(t: str) -> dict | None:
    if re.fullmatch(rf"(mute|silence){_VOL_OBJ}", t):
        return {"action": "mute"}
    if re.fullmatch(rf"un-?mute{_VOL_OBJ}", t):
        return {"action": "unmute"}
    if re.fullmatch(
        r"(what('?s| is)( the)?( current)? volume|current volume|"
        r"volume level|how loud is it)", t):
        return {"action": "get"}

    m = re.fullmatch(_VOL_UP + _VOL_BY, t)
    if m:
        return _vol_by(m, +1)
    m = re.fullmatch(_VOL_DOWN + _VOL_BY, t)
    if m:
        return _vol_by(m, -1)
    if re.fullmatch(r"(halve|cut in half)( the (volume|sound))?|cut the (volume|sound) in half", t):
        return {"action": "adjust", "percent": -50}
    if re.fullmatch(r"double( the (volume|sound))?", t):
        return {"action": "adjust", "percent": 100}

    m = re.fullmatch(
        r"(?:set |change |make |put )?(?:the )?volume(?: level)? "
        r"(?:to |at |= ?)?(\d{1,3})(?: ?%| ?percent)?", t)
    if m:
        return {"action": "set", "level": max(0, min(100, int(m.group(1))))}
    if re.fullmatch(r"(?:set |put )?(?:the )?volume (?:to )?(?:max|maximum|full|all the way up)", t):
        return {"action": "set", "level": 100}
    if re.fullmatch(r"(?:set |put )?(?:the )?volume (?:to )?(?:min|minimum|zero)", t):
        return {"action": "set", "level": 0}
    if re.fullmatch(r"max(?:imum)? volume|full volume|full blast", t):
        return {"action": "set", "level": 100}

    if re.fullmatch(
        rf"(turn (it |the volume |the sound )?up|turn up( the volume| the sound| it)?|"
        rf"volume up|louder|crank it( up)?|bump it up|pump it up|more volume|"
        rf"increase (the )?volume|up{_VOL_OBJ}){_VOL_HEDGE}", t):
        return {"action": "adjust", "delta": 10}
    if re.fullmatch(
        rf"(turn (it |the volume |the sound )?down|turn down( the volume| the sound| it)?|"
        rf"volume down|quieter|softer|lower (the )?volume|less volume|"
        rf"decrease (the )?volume|down{_VOL_OBJ}){_VOL_HEDGE}", t):
        return {"action": "adjust", "delta": -10}
    if re.fullmatch(r"(way|much|a lot) (louder|higher|up)", t):
        return {"action": "adjust", "delta": 25}
    if re.fullmatch(r"(way|much|a lot) (quieter|lower|softer|down)", t):
        return {"action": "adjust", "delta": -25}
    return None


_MEDIA_VERB_VOCAB = (
    "increase decrease raise lower reduce boost bump crank drop cut halve double "
    "turn set make put mute unmute silence volume louder quieter softer "
    "pause stop hold resume unpause continue play start next skip forward "
    "previous prev rewind"
).split()


def _damerau_levenshtein(a: str, b: str) -> int:
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def _repair_first_word(t: str) -> str:
    words = t.split()
    if not words or words[0] in _MEDIA_VERB_VOCAB or not words[0].isalpha():
        return t
    w = words[0]
    budget = 1 if len(w) <= 6 else 2
    best, best_d = None, budget + 1
    for cand in _MEDIA_VERB_VOCAB:
        if cand[0] != w[0] or abs(len(cand) - len(w)) > budget:
            continue
        d = _damerau_levenshtein(w, cand)
        if d < best_d:
            best, best_d = cand, d
    if best is not None and best_d <= budget:
        words[0] = best
        return " ".join(words)
    return t


_PLAY_NOT_A_QUERY = {
    "music", "the music", "it", "some music", "the song", "the track",
    "again", "spotify", "something", "some", "on", "it again", "that again",
    "this again", "the next song", "next song", "the previous song",
}


def _fast_play_query(t: str) -> str | None:
    m = re.fullmatch(
        r"(?:play(?: me| us)?|(?:put|throw|chuck) on some|start playing)\s+(.+)", t)
    if not m:
        return None
    q = re.sub(r"\s+(on spotify|please|now|for me)$", "", m.group(1)).strip()
    if not q or q in _PLAY_NOT_A_QUERY:
        return None
    if re.fullmatch(r"(the )?(next|previous|last|first) (song|track|one)", q):
        return None
    # "play X and turn it up" / "play jazz then pause" — a compound request, not
    # a single deterministic command. Let the model handle it.
    if re.search(r"\b(and|then|also|after that)\s+"
                 r"(turn|set|make|skip|pause|stop|play|lower|raise|increase|"
                 r"decrease|mute|unmute|open|search|remind|add)\b", q):
        return None
    return q


# ── reminder time parsing ────────────────────────────────────────────────────

_DAY_WORD = r"(?:today|tonight|tomorrow)"

_RELATIVE_TIME_RE = re.compile(
    r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
_NOON_MIDNIGHT_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*\b(noon|midnight)\b\s*({_DAY_WORD})?\b", re.IGNORECASE)
_CLOCK_AMPM_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*(?:\bat\b\s*)?(\d{{1,2}})(?::(\d{{2}}))?\s*([ap]\.?m\.?)\s*({_DAY_WORD})?\b",
    re.IGNORECASE)
_CLOCK_24H_RE = re.compile(
    rf"\b({_DAY_WORD})?\s*(?:\bat\b\s*)?([01]?\d|2[0-3]):([0-5]\d)\s*({_DAY_WORD})?\b",
    re.IGNORECASE)


def _roll_day(dt, day_word: str | None, now):
    day_word = (day_word or "").lower()
    if day_word == "tomorrow":
        return dt + timedelta(days=1)
    if day_word in ("today", "tonight"):
        return dt
    if dt <= now:
        return dt + timedelta(days=1)
    return dt


def _parse_reminder_time(text: str, now):
    m = _RELATIVE_TIME_RE.search(text)
    if m:
        n = int(m.group(1))
        mins = n * 60 if m.group(2).lower().startswith("h") else n
        dt = now + timedelta(minutes=mins)
        return dt, (text[:m.start()] + text[m.end():])

    m = _NOON_MIDNIGHT_RE.search(text)
    if m:
        hour = 12 if m.group(2).lower() == "noon" else 0
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(3), now)
        return dt, (text[:m.start()] + text[m.end():])

    m = _CLOCK_AMPM_RE.search(text)
    if m:
        hour = int(m.group(2)) % 12
        minute = int(m.group(3) or 0)
        if m.group(4).lower().replace(".", "") == "pm":
            hour += 12
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(5), now)
        return dt, (text[:m.start()] + text[m.end():])

    m = _CLOCK_24H_RE.search(text)
    if m:
        dt = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
        dt = _roll_day(dt, m.group(1) or m.group(4), now)
        return dt, (text[:m.start()] + text[m.end():])

    return None, text


_REMINDER_CONNECTOR_RE = re.compile(
    r"^(to|about|for|on|at|that says|saying|titled|called)\s+", re.IGNORECASE)
_AMBIGUOUS_BARE_NUM_RE = re.compile(r"\b(?:at|by)\s+\d{1,2}\b", re.IGNORECASE)
_REMINDER_STOPWORDS = {
    "to", "about", "for", "on", "at", "that", "saying", "titled", "called", "it", "this",
}


def _fast_reminder_call(text: str) -> tuple[str, dict] | None:
    t = text.strip()
    t = re.sub(r"^(hey |ok |okay )?(bella|jarvis)[\s,:]+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(please|can you|could you|would you)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\s.!?,;]+$", "", t).strip()

    m = re.match(r"^remind me\s+(?:to\s+)?(.+)$", t, flags=re.IGNORECASE)
    if not m:
        m = re.match(
            r"^(?:set|add|create)\s+(?:a|an)?\s*reminders?\s*"
            r"(?:to|about|for|that says|saying|titled|called)?\s*(.+)$",
            t, flags=re.IGNORECASE,
        )
    if not m:
        return None

    rest = m.group(1).strip()
    if not rest:
        return None

    now = datetime.now()
    due_dt, remaining = _parse_reminder_time(rest, now)

    if due_dt is None and re.search(rf"\b{_DAY_WORD}\b", rest, flags=re.IGNORECASE):
        return None
    if due_dt is None and _AMBIGUOUS_BARE_NUM_RE.search(rest):
        return None

    remaining = remaining.strip(" ,.")
    remaining = _REMINDER_CONNECTOR_RE.sub("", remaining)
    remaining = re.sub(r"\s{2,}", " ", remaining).strip(" ,.")
    if not remaining or remaining.lower() in _REMINDER_STOPWORDS:
        return None

    inp = {"action": "add", "text": remaining}
    if due_dt:
        inp["due_date"] = due_dt.strftime("%Y-%m-%d %H:%M")
    return "reminder", inp


def normalize(text: str) -> str:
    """Lowercase, drop a leading/trailing wake word and a leading politeness
    prefix. The one canonical form the classifier reasons about. Greeting words
    ("hey", "hi") are kept — they're signal, not noise."""
    t = text.strip().lower()
    t = re.sub(r"[\s,]+(bella|jarvis)\b[\s.!?]*$", "", t).strip()
    t = re.sub(r"^(hey|hi|ok|okay|yo)?[\s,]*(bella|jarvis)\b[\s,:.!?]*", "", t).strip()
    t = re.sub(r"^(please|pls|can you|could you|would you|will you|kindly)\s+", "", t).strip()
    t = re.sub(r"[\s.!?,;]+$", "", t).strip()
    return t


def _fast_media_call(text: str) -> tuple[str, dict] | None:
    """Return (tool_name, tool_input) for an unambiguous media command, else None."""
    t = text.strip().lower()
    t = re.sub(r"^(hey |ok |okay )?(bella|jarvis)[\s,:]+", "", t)
    t = re.sub(r"^(please|can you|could you|would you)\s+", "", t)
    t = re.sub(r"[\s.!?,;]+$", "", t).strip()
    t = _repair_first_word(t)

    for pattern, action in _SPOTIFY_FAST_RULES:
        if re.fullmatch(pattern, t):
            return "spotify", {"action": action}

    query = _fast_play_query(t)
    if query:
        return "spotify", {"action": "play", "query": query}

    vol = _fast_volume_input(t)
    if vol:
        return "volume", vol
    return None


# ── Tier 0: trivial conversational responses (no model) ─────────────────────

# Deliberately small. These are *low-information* messages where any competent
# reply is interchangeable — greetings, acknowledgements, sign-offs. Anything
# with real content falls through to the model.

_GREETING = {
    "hi", "hii", "hey", "hey", "hello", "helo", "hiya", "heya", "yo", "sup",
    "hey there", "hi there", "hello there", "howdy", "hey bella", "hi bella",
    "hello bella", "good morning", "morning", "good afternoon", "afternoon",
    "good evening", "evening",
}
_MORNING = {"good morning", "morning", "gm"}
_AFTERNOON = {"good afternoon", "afternoon"}
_EVENING = {"good evening", "evening"}

_THANKS = {
    "thanks", "thank you", "thankyou", "thx", "ty", "tysm", "thanks a lot",
    "thanks so much", "thank you so much", "much appreciated", "appreciate it",
    "appreciated", "cheers", "cheers bella", "thanks bella", "thank you bella",
    "ok thanks", "okay thanks", "great thanks",
}
_ACK = {
    "ok", "okay", "k", "kk", "alright", "aight", "got it", "gotcha",
    "understood", "sounds good", "makes sense", "noted", "fair enough",
    "right", "i see", "cool cool",
}
_PRAISE = {"nice", "cool", "great", "awesome", "perfect", "sweet", "excellent",
           "brilliant", "love it", "amazing", "beautiful"}
_FAREWELL = {
    "bye", "goodbye", "bye bye", "byebye", "see you", "see ya", "see you later",
    "cya", "later", "laters", "catch you later", "talk later", "talk soon",
    "gtg", "g2g", "i'm out", "im out", "take care", "peace", "peace out",
}
_NIGHT = {"good night", "goodnight", "night", "nighty night", "gn"}
_FILLER = {"lol", "lmao", "lmfao", "rofl", "haha", "hahaha", "hehe", "heh",
           "hah", "lolol"}

# Acks that could plausibly be *answering a question Bella just asked*
# ("Proceed? " -> "ok") — suppressed when the previous turn was a question.
_STATEFUL_ACKS = _ACK | {"sure", "yes", "yeah", "yep", "yup", "no", "nope",
                         "nah", "go ahead", "do it", "please do"}


def _local_response(t: str, *, last_was_question: bool) -> str | None:
    if t == "":
        return "Hey. What's up?"  # bare wake word ("Bella?")
    if t in _MORNING:
        return "Morning. What's on?"
    if t in _AFTERNOON:
        return "Afternoon. What's up?"
    if t in _EVENING:
        return "Evening. What do you need?"
    if t in _NIGHT:
        return "Night."
    if t in _GREETING:
        return "Hey. What's up?"
    if t in _FAREWELL:
        return "See you."
    if t in _FILLER:
        return "Heh."
    if last_was_question and t in _STATEFUL_ACKS:
        return None  # might be answering Bella — let the model handle it
    if t in _THANKS:
        return "Anytime."
    if t in _ACK:
        return "Got it."
    if t in _PRAISE:
        return "Glad it landed."
    return None


# ── Tier 0: a few more deterministic tool commands ──────────────────────────

def _fast_list_reminders(t: str) -> tuple[str, dict] | None:
    if re.fullmatch(r"(list|show( me)?|what are|check|see|read out)"
                    r"( my| the| all| upcoming)* reminders", t):
        return "reminder", {"action": "list"}
    if re.fullmatch(r"(list|show( me)?|what are|check|see)"
                    r"( my| the| all)* (todos|tasks|to-?dos?|task list)", t):
        return "todo", {"action": "list"}
    return None


# Only an explicit, whole-string request to *run* git status — never a question
# ABOUT git status ("what does git status mean", "explain git status").
_GIT_READONLY = {
    "git status": "git status",
    "git diff": "git diff",
    "git log": "git log --oneline -15",
    "git branch": "git branch",
}


def _fast_git_readonly(t: str) -> tuple[str, dict] | None:
    m = re.fullmatch(
        r"(?:run |exec(?:ute)? |do |show(?: me)? |check |get |give me |what'?s |whats )?"
        r"(?:a |the |my |current )?"
        r"(git status|git diff|git log|git branch)"
        r"(?: for (?:this|the) (?:repo|repository|project))?",
        t,
    )
    if not m:
        return None
    return "shell", {"command": _GIT_READONLY[m.group(1)]}


# ── FAST_CHAT vs FULL_AGENT signals ────────────────────────────────────────

_URL_RE = re.compile(r"https?://|www\.\w|\b\w+\.(com|org|net|io|dev|ai|gov|edu)\b", re.I)
_PATH_RE = re.compile(r"(^|\s)(~|\.{1,2})?/[\w.\-/]+|\b[\w\-]+\.(py|js|ts|tsx|jsx|json|ya?ml|toml|md|sh|rs|go|cpp|hpp|c|h|txt|csv|html|css|sql|ipynb|env|cfg|ini|lock|dockerfile)\b", re.I)
_CODE_FENCE_RE = re.compile(r"```|\n {4}\S")

# Strong signal the model will need a tool. Word-boundary matches only.
_TOOL_SIGNAL_RE = re.compile(
    r"\b("
    r"search|google|look\s?up|lookup|find\s+(out|me)|browse|"
    r"weather|forecast|news|headlines|price|stock|scores?|"
    r"latest|currently|right\s+now|today'?s|"
    r"run|execute|compile|rebuild|deploy|install|uninstall|"
    r"npm|pnpm|yarn|pip|pipx|cargo|docker|kubectl|make|brew|"
    r"commit|push|pull\s+request|rebase|checkout|stash|clone|"
    r"github|repo|repository|codebase|readme|"
    r"open|launch|visit|navigate|download|"
    r"file|files|folder|directory|script|logs?|config|"
    r"create|write|edit|append|delete|remove|rename|move|touch|mkdir|chmod|"
    r"remind|reminder|schedule|alarm|todo|task\s+list|"
    r"spotify|playlist|album|"
    r"remember\s+(that|this)|recall|forget\s+that|note\s+that|"
    r"debug|traceback|stack\s?trace|exception|failing|crash|restart|"
    r"screenshot|clipboard|"
    r"upload|back\s+(this\s+|it\s+)?up|sync"
    r")\b",
    re.I,
)
# "this / my <thing>" — references the environment, needs a tool to inspect.
_ENV_REF_RE = re.compile(
    r"\b(this|these|my|our|the)\s+"
    r"(repo|repository|project|codebase|code|file|files|folder|directory|"
    r"function|class|module|script|error|bug|test|tests|output|log|logs|"
    r"container|service|server|build|branch|diff|commit|README|doc|docs)\b",
    re.I,
)

# Knowledge / language questions — safe for FAST_CHAT even if a tool-ish word
# appears ("explain what git status does"). Requires no environment reference.
_KNOWLEDGE_RE = re.compile(
    r"^(explain|define|what\s+(is|are|does|do|means?)|what'?s|whats|"
    r"how\s+(does|do|would|can)|why\s+(does|do|is|are|would)|"
    r"tell\s+me\s+about|describe|meaning\s+of|difference\s+between|"
    r"pros\s+and\s+cons|compare|translate|how\s+do\s+you\s+say|"
    r"give\s+me\s+an?\s+example|summar(y|ise|ize)|rephrase|rewrite|"
    r"proofread|correct)\b",
    re.I,
)
_SMALLTALK_RE = re.compile(
    r"^(how\s+(are|r)\s+(you|u|ya)|how'?s\s+it\s+going|how\s+are\s+things|"
    r"how\s+do\s+you\s+feel|you\s+(ok|okay|good|there)|what'?s\s+up|whats\s+up|"
    r"how\s+was\s+your|are\s+you\s+(ok|okay|there|awake)|"
    r"tell\s+me\s+a\s+joke|another\s+joke|say\s+something|make\s+me\s+laugh|"
    r"who\s+are\s+you|what\s+are\s+you|what'?s\s+your\s+name|"
    r"what\s+can\s+you\s+do|introduce\s+yourself)\b",
    re.I,
)
_DEEP_RE = re.compile(
    r"\b(in\s+depth|in\s+detail|detailed|comprehensive|thorough|step[\s-]by[\s-]step|"
    r"deep\s+dive|essay|long|walk\s+me\s+through|full\s+explanation|"
    r"research|investigate|analy[sz]e|refactor|implement|design|architect)\b",
    re.I,
)


# Time-sensitive / external-world nouns — these essentially always need a live
# lookup, so they override the "knowledge question" exemption.
_CURRENT_DATA_RE = re.compile(
    r"\b(weather|forecast|temperature|humidity|rain|snow|"
    r"news|headlines?|breaking|"
    r"stock|shares?\s+price|share\s+price|price\s+of|exchange\s+rate|"
    r"crypto|bitcoin|ethereum|market\s+cap|"
    r"who\s+won|who\s+is\s+winning|final\s+score|match\s+score|"
    r"latest\s+(version|release|update|news)|"
    r"current\s+(president|ceo|price|version|time\s+in)|"
    r"release\s+date\s+of)\b",
    re.I,
)


def _has_env_context(raw: str) -> bool:
    return bool(
        _URL_RE.search(raw) or _PATH_RE.search(raw)
        or _CODE_FENCE_RE.search(raw) or _ENV_REF_RE.search(raw)
    )


def _needs_tools(raw: str, t: str) -> bool:
    if _has_env_context(raw) or _CURRENT_DATA_RE.search(raw):
        return True
    # Pure knowledge / small-talk question, no environment reference -> no tools.
    if _KNOWLEDGE_RE.match(t) or _SMALLTALK_RE.match(t):
        return False
    return bool(_TOOL_SIGNAL_RE.search(raw))


def _tool_groups_for(raw: str) -> list[str]:
    groups: list[str] = []
    checks = {
        "media": r"\b(spotify|song|track|music|playlist|album|artist|play|paus|"
                 r"skip|volume|mute|unmute|louder|quieter)\b",
        "web": r"\b(search|google|look\s?up|weather|news|price|stock|browse|"
               r"website|visit|open)\b|https?://",
        "memory": r"\b(memory|remember|recall|forget|note\s+that|what\s+did\s+(we|i)|"
                  r"last\s+time|previously|earlier)\b",
        "reminders": r"\b(remind|reminder|schedule|alarm|todo|task)\b",
        "coding": r"\b(run|execute|compile|build|deploy|install|npm|pnpm|yarn|pip|"
                  r"cargo|docker|git|commit|push|github|repo|repository|codebase|"
                  r"file|files|directory|folder|script|debug|traceback|test|tests|"
                  r"error|bug|log|logs|read|write|create|edit)\b|```",
    }
    for group, pat in checks.items():
        if re.search(pat, raw, re.I):
            groups.append(group)
    if _has_env_context(raw) and "coding" not in groups:
        groups.append("coding")
    return groups


def _profile_for_agent(raw: str, groups: list[str]) -> str:
    if _DEEP_RE.search(raw) or _CODE_FENCE_RE.search(raw) or len(raw) > 260:
        return "DEEP"
    if "coding" in groups:
        return "DEEP"
    if groups and set(groups) <= {"media", "reminders", "memory"}:
        return "NORMAL"
    return "NORMAL"


def _profile_for_chat(raw: str, t: str) -> str:
    if _DEEP_RE.search(raw) or len(raw) > 220:
        return "DEEP"
    if _SMALLTALK_RE.match(t) or len(t) <= 24:
        return "FAST"
    return "NORMAL"


_SLASH_KNOWN = {
    "/help", "/status", "/doctor", "/tools", "/memory", "/history", "/recall",
    "/clear", "/listen", "/silent", "/tts", "/verbose", "/benchmark",
    "/exit", "/quit",
}


# ── public API ──────────────────────────────────────────────────────────────

def classify(user_input: str, *, last_was_question: bool = False) -> RouteDecision:
    """Route a request without ever calling a model. See the module header."""
    raw = user_input.strip()
    if not raw:
        return RouteDecision(Route.FULL_AGENT, 0.0, reason="empty input")

    # 1. Slash commands never touch the model.
    if raw.startswith("/"):
        cmd = raw.split(None, 1)[0].lower()
        known = cmd in _SLASH_KNOWN
        return RouteDecision(
            Route.SLASH_COMMAND, 1.0 if known else 0.5,
            tool_name=cmd,
            response=None if known else f"Unknown command: {cmd}",
            reason="slash command",
        )

    t = normalize(raw)

    # 2. Deterministic tool commands (whole-string, high confidence).
    hit = (
        _fast_media_call(raw)
        or _fast_reminder_call(raw)
        or _fast_list_reminders(t)
        or _fast_git_readonly(t)
    )
    if hit is not None:
        return RouteDecision(
            Route.DIRECT_TOOL, 0.97, tool_name=hit[0], tool_args=hit[1],
            reason=f"direct tool: {hit[0]}",
        )

    # 3. Trivial conversational — canned reply, no model.
    canned = _local_response(t, last_was_question=last_was_question)
    if canned is not None:
        return RouteDecision(
            Route.LOCAL_RESPONSE, 0.95, response=canned,
            reason="trivial conversational", profile="FAST",
        )

    # 4. Needs a tool? -> agent. Otherwise -> lightweight chat.
    if _needs_tools(raw, t):
        groups = _tool_groups_for(raw)
        return RouteDecision(
            Route.FULL_AGENT,
            0.8 if groups else 0.45,
            tool_groups=groups,
            reason=("tool groups: " + ", ".join(groups)) if groups else "tools needed (full set)",
            profile=_profile_for_agent(raw, groups),
        )

    return RouteDecision(
        Route.FAST_CHAT, 0.7, reason="no tool signal",
        profile=_profile_for_chat(raw, t),
    )


def match(user_input: str) -> RouterMatch | None:
    """Back-compat: return a DIRECT_TOOL match or None. Prefer classify()."""
    d = classify(user_input)
    if d.route is Route.DIRECT_TOOL:
        return RouterMatch(tool=d.tool_name, arguments=d.tool_args)
    return None


def format_reply(tool: str, inp: dict, result: dict) -> str:
    """Turn a fast-path tool result into a short spoken/printed reply."""
    if tool == "shell":
        # Direct read-only command (e.g. `git status`) — the output *is* the answer.
        if result.get("error"):
            return f"Couldn't run that — {result['error']}"
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        if result.get("exit_code", 0) != 0:
            return (err or out or "command failed").strip()[:2000]
        return (out or err or "(no output)")[:2000]

    if tool == "reminder" and inp.get("action") == "list":
        rems = result.get("reminders", [])
        if not rems:
            return "No upcoming reminders."
        lines = [f"• {r['name']}" + (f" — {r['due']}" if r.get("due") else "") for r in rems]
        return "\n".join(lines)

    if tool == "todo" and inp.get("action") == "list":
        todos = result.get("todos", [])
        if not todos:
            return "Your task list is empty."
        return "\n".join(
            f"{i}. {'✓' if td.get('done') else '○'} {td['text']}" for i, td in enumerate(todos, 1)
        )

    if not result.get("success"):
        err = (result.get("error") or result.get("stderr")
               or f"{tool} didn't respond.")
        return f"Couldn't do that — {err}"

    if tool == "reminder":
        action = inp["action"]
        if action == "add":
            due = result.get("due_date")
            when = f" for {due}" if due else ""
            return f'Reminder added: "{result.get("added", inp.get("text", ""))}"{when}.'
        return "Done."

    if tool == "spotify":
        action = inp["action"]
        track = (result.get("track") or "").strip(" —")
        state = result.get("player_state", "")
        if action == "pause":
            return "Paused." if state != "playing" else "Sent pause, but Spotify still shows playing."
        if action == "play":
            if not track:
                return "Playing."
            if state and state != "playing":
                return f"Loaded {track}, but Spotify shows it {state}."
            return f"Now playing {track}."
        if action in ("next", "previous"):
            return f"{'Skipped' if action == 'next' else 'Went back'}{f' — now playing {track}' if track else '.'}"
        if action == "current":
            if not track:
                return "Nothing's playing."
            return track if state == "playing" else f"{track} ({state or 'stopped'})"
        return "Done."

    # volume
    action = inp["action"]
    vol = result.get("volume")
    muted = result.get("muted")
    tag = " (muted)" if muted else ""
    if action == "mute":
        return "Muted."
    if action == "unmute":
        return f"Unmuted — volume {vol}."
    if action == "get":
        return f"Volume is {vol}{tag}."
    prev = result.get("previous_volume")
    if action == "adjust" and prev is not None:
        return f"Volume {prev} → {vol}{tag}."
    return f"Volume set to {vol}{tag}."


# Legacy aliases (jarvis.py / server.py compatibility).
_fast_media_reply = format_reply
