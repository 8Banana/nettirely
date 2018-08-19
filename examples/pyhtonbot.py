#!/usr/bin/env python3
"""
The 8Banana team's own bot.
We actually use this one on the #8Banana IRC channel :)
"""

import collections
import logging
import logging.handlers
import datetime
import os
import random
import re
import sys
import time
import urllib.parse

import asks
import curio
from curio import subprocess

from supervisor import Supervisor
from nettirely import IrcBot, NO_SPLITTING

# General bot constants
ADMINS = {"__Myst__", "theelous3", "Akuli", "Zaab1t"}
TRUSTED = {"marky1991", "darkf", "go|dfish", "stuzz"}
COMBINED_USERS = ADMINS | TRUSTED

# Last seen constants
TIME_AMOUNTS = ("second", "minute", "hour")

# Fish slapper constants
SLAP_TEMPLATE = "slaps {slappee} around a bit with {fish}"
FISH = (
    "asyncio",
    "multiprocessing",
    "twisted",
    "django",
    "pathlib",
    "python 2.7",
    "a daemon thread",
    "unittest",
    "logging",
    "xml.parsers.expat.XML_PARAM_ENTITY_PARSING_UNLESS_STANDALONE",
    "urllib.request.HTTPPasswordMgrWithDefaultRealm",
    "javascript",
)

# Markov chain constants
EMPTY_WORD = "__EMPTY__"
PRECEDING_WORDS = 2
JSON_TUPLE_SEPARATOR = "\0"

# <Logger initialization>
handlers = []

stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(
    logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
)
handlers.append(stream_handler)
del stream_handler

file_handler = logging.handlers.TimedRotatingFileHandler(
    "pyhtonbot.log", when="D", interval=1, backupCount=7
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(
    logging.Formatter("[%(levelname)s @ %(asctime)-15s] %(name)s: %(message)s")
)
handlers.append(file_handler)
del file_handler

# Needed to set the "max level" for the above handlers. If we don't set this to
# DEBUG but for example to INFO, all the handlers are limited to INFO.
logging.basicConfig(handlers=handlers, level=logging.DEBUG)

del handlers
# </Logger initialization>

bot = IrcBot(state_path="pyhtonbot_state.json")
supervisor = Supervisor("8Banana/nettirely")


@bot.on_command(">>>", NO_SPLITTING)
@bot.on_command("!py", NO_SPLITTING)
async def annoy_raylu(self, _, recipient, text):
    if recipient == self.nick:
        return
    await self.send_privmsg(recipient, "!py3 " + text)


@bot.on_command("!slap", 1)
async def slap(self, _, recipient, slappee):
    fish = random.choice(FISH)
    await self.send_action(
        recipient, SLAP_TEMPLATE.format(slappee=slappee, fish=fish)
    )


@bot.on_connect
async def initialize_logs(self):
    logs = collections.defaultdict(lambda: collections.deque(maxlen=500))

    if "logs" in self.state:
        logs.update(
            {
                k: collections.deque(v, maxlen=500)
                for k, v in self.state["logs"].items()
            }
        )

    self.state["logs"] = logs


@bot.on_disconnect
def save_logs(self):
    self.state["logs"] = {
        k: list(v) for k, v in self.state.get("logs", {}).items()
    }


async def upload_log(lines):
    resp = await asks.post(
        "https://theelous3.net/irc_log", data="\n".join(lines)
    )
    return resp.text


@bot.on_join
async def append_join_to_log(self, sender, channel):
    logs = self.state["logs"]
    now = datetime.datetime.now().strftime("%X")
    logs[channel].append(f"[{now}] {sender.nick} joined {channel}")


@bot.on_part
async def append_part_to_log(self, sender, channel, reason=None):
    if reason is None:
        reason = "No reason."

    logs = self.state["logs"]
    now = datetime.datetime.now().strftime("%X")
    logs[channel].append(f"[{now}] {sender.nick} parted {channel} ({reason})")


@bot.on_quit
async def append_quit_to_log(self, sender, reason=None):
    logs = self.state["logs"]

    if reason is None:
        reason = "No reason."
    now = datetime.datetime.now().strftime("%X")
    msg = f"[{now}] {sender.nick} quit ({reason})"

    for channel, users in self.channel_users.items():
        if sender.nick in users:
            logs[channel].append(msg)


@bot.on_privmsg
async def append_privmsg_to_log(self, sender, recipient, message):
    logs = self.state["logs"]

    now = datetime.datetime.now().strftime("%X")

    # If we are the reply recipient, this means we are in a private message
    # conversation. We want to change the key to the logs dictionary to be the
    # sender's nickname, else we would group all private messages under one
    # key. We don`t need to adjust the key later, in `send_log`, because the
    # reply recipient is already the sender's nick or a channel.
    if recipient == self.nick:
        recipient = sender.nick

    logs[recipient].append(f"[{now}] <{sender.nick}> {message}")


@bot.on_command("!log", 0)
async def send_log(self, sender, recipient):
    logs = self.state["logs"]

    if logs[recipient]:
        await self.send_privmsg(
            recipient,
            f"{sender.nick}: Uploading logs, this might take a second...",
        )
        result = await upload_log(logs[recipient])
        await self.send_privmsg(recipient, f"{sender.nick}: {result}")
    else:
        await self.send_privmsg(
            recipient, f"{sender.nick}: There are no logs to be uploaded."
        )


FREENODE_SPAM_PREFIXES = [
    "After the acquisition by Private Internet Access, Freenode is now being "
    "used to push ICO scams ",
    'Christel just posted this "denial" on the freenode',
    "Consider Andrew Lee's involvement, Andrew Lee is Christel's "
    "boss at London Trust Media",
]


@bot.on_connect
async def initialize_spammer_database(self):
    if "spammer_prefixes" in self.state:
        self.state["spammer_regexps"] = [
            "^" + re.escape(prefix)
            for prefix in self.state.pop("spammer_prefixes")
        ]
    else:
        self.state.setdefault(
            "spammer_regexps",
            ["^" + re.escape(prefix) for prefix in FREENODE_SPAM_PREFIXES],
        )


@bot.on_command("!addspammer", 1)
async def add_spammer(self, sender, source, spammer_nickname):
    source_logs = self.state["logs"][source]
    for line in source_logs:
        if " <" in line and "> " in line:
            nickname, message = line.split(" <", 1)[1].split("> ", 1)
            if nickname == spammer_nickname:
                first_spammer_message = message
                break
    else:  # no break
        await self.send_privmsg(
            source, f"Could not find a first message for {spammer_nickname!r}"
        )
        return

    await self.send_privmsg(
        source, f"Added {first_spammer_message!r} as a prefix."
    )
    self.state["spammer_regexps"].append(
        "^" + re.escape(first_spammer_message)
    )


@bot.on_command("!addspamregexp", NO_SPLITTING)
async def add_spam_regexp(self, sender, source, spam_regexp):
    if sender.nick in COMBINED_USERS:
        self.state["spammer_regexps"].append(spam_regexp)
        await self.send_privmsg(source, f"Added {spam_regexp!r} as a regexp.")


@bot.on_command("!removespamregexp", NO_SPLITTING)
async def remove_spam_regexp(self, sender, source, spam_regexp):
    if sender.nick in COMBINED_USERS:
        try:
            self.state["spammer_regexps"].remove(spam_regexp)
        except ValueError:
            pass


@bot.on_privmsg
async def kick_spammers(self, sender, channel, message):
    for spam_pattern in self.state["spammer_regexps"]:
        if re.search(spam_pattern, message) is not None:
            await self.kick(channel, sender.nick, "spamming detected")


@bot.on_command("!spammer_regexps", NO_SPLITTING)
async def send_spammer_regexps(self, sender, source, _):
    for index, regexp in enumerate(self.state["spammer_regexps"]):
        await self.send_privmsg(source, f"{index + 1}. {regexp!r}")


def _pick_word(word_frequencies):
    population = []
    weights = []

    for word, frequency in word_frequencies.items():
        population.append(word)
        weights.append(frequency)

    return random.choices(population, weights)[0]


@bot.on_connect
async def initialize_markov_chains(self):
    # These two methods are what I like to call "unholy."
    self.state["markov_chains"] = {
        user: {
            tuple(k.split(JSON_TUPLE_SEPARATOR)): v for k, v in chain.items()
        }
        for user, chain in self.state.get("markov_chains", {}).items()
    }


@bot.on_disconnect
def save_markov_chains(self):
    self.state["markov_chains"] = {
        user: {JSON_TUPLE_SEPARATOR.join(k): v for k, v in chain.items()}
        for user, chain in self.state.get("markov_chains", {}).items()
    }


def _train_markov_chain(markov_chain, text):
    words = (
        [EMPTY_WORD] * PRECEDING_WORDS
        + text.split()
        + [EMPTY_WORD] * PRECEDING_WORDS
    )

    for i in range(0, len(words) - PRECEDING_WORDS - 1):
        *preceding, word = words[i : i + PRECEDING_WORDS + 1]

        prefix = markov_chain.setdefault(tuple(preceding), {})

        if JSON_TUPLE_SEPARATOR in word:
            continue

        prefix[word] = prefix.get(word, 0) + 1


def _reconstruct_text(markov_chain):
    words = [EMPTY_WORD] * PRECEDING_WORDS
    result = []

    while True:
        word = _pick_word(markov_chain[tuple(words)])

        if word == EMPTY_WORD:
            break

        result.append(word)
        words = words[1:]
        words.append(word)

    return " ".join(result)


@bot.on_privmsg
async def update_markov_chain(self, sender, _channel, message):
    markov_chains = self.state.setdefault("markov_chains", {})
    markov_chain = markov_chains.setdefault(sender.nick, {})
    _train_markov_chain(markov_chain, message)


@bot.on_command("!parrot", 1)
async def parrot(self, sender, channel, user):
    markov_chains = self.state.setdefault("markov_chains", {})
    markov_chain = markov_chains.setdefault(user, {})

    if markov_chain:
        mimic = _reconstruct_text(markov_chain)

        await self.send_privmsg(channel, f"{sender.nick}: {mimic}")
    else:
        await self.send_privmsg(
            channel, f"{sender.nick}: I don't know how to parrot {user}."
        )


@bot.on_privmsg
async def update_seen(self, sender, _channel, message):
    seen = self.state.setdefault("seen", {})
    seen[sender.nick] = time.time()


@bot.on_command("!seen", 1)
async def show_seen(self, sender, channel, user):
    seen = self.state.get("seen", {})

    if user in seen:
        when = time.time() - seen[user]

        for n, amount in enumerate(TIME_AMOUNTS):
            if when < 60 ** (n + 1):
                break

        when //= 60 ** n

        if when != 1:
            amount += "s"

        if when == 0:
            when = "right now"
        elif when < 0:
            when = "on Thursday, 1 January 1970"
        else:
            when = f"{when} {amount} ago"

        await self.send_privmsg(
            channel, f"{sender.nick}: {user} was last seen {when}."
        )
    else:
        await self.send_privmsg(
            channel, f"{sender.nick}: I've never seen {user}."
        )


def _make_url(domain, what2google):
    # example response: 'http://www.lmfgtfy.com/?q=wolo+wolo'
    params = urllib.parse.urlencode({"q": what2google})
    return "http://www.%s/?%s" % (domain, params)


async def _respond(self, recipient, domain, text):
    if recipient == self.nick:
        return

    try:
        target, what2google = text.split(maxsplit=1)
    except ValueError:
        command = "fgoogle" if domain == "lmfgtfy.com" else "google"
        await self.send_privmsg(
            recipient, "Usage: !%s nick what2google" % command
        )
        return

    url = _make_url(domain, what2google)

    await self.send_privmsg(recipient, "%s: %s" % (target, url))


@bot.on_command("!google", NO_SPLITTING)
async def google(self, _, recipient, text):
    await _respond(self, recipient, "lmgtfy.com", text)


@bot.on_command("!fgoogle", NO_SPLITTING)
async def fgoogle(self, _, recipient, text):
    await _respond(self, recipient, "lmfgtfy.com", text)


@bot.on_command("!autolog", 1)
async def autolog(self, sender, recipient, argument):
    argument = argument.lower()

    self.state.setdefault("autologgers", [])
    if argument == "on":
        self.state["autologgers"].append(sender.nick)
        await self.send_privmsg(
            recipient, f"{sender.nick}: You will recieve logs automatically."
        )
    elif argument == "off":
        if sender.nick in self.state["autologgers"]:
            self.state["autologgers"].remove(sender.nick)
        await self.send_privmsg(
            recipient,
            f"{sender.nick}: You will not recieve logs automatically anymore.",
        )
    else:
        await self.send_privmsg(
            recipient, f"{sender.nick}: USAGE: !autolog on/off"
        )


@bot.on_join
async def autolog_send(self, sender, channel):
    if sender.nick in self.state.get("autologgers", ()):
        logs = self.state["logs"]
        result = await upload_log(logs[channel])

        # We do a weird trick here.
        # Some clients show NOTICEs of the form "[CHANNELNAME] NOTICE" in the
        # channel buffer named by CHANNELNAME.
        # We abuse this here to make the logs show up in the channel itself.
        await self.send_notice(sender.nick, f"[{channel}] Logs: {result}")


@bot.on_command("!update", NO_SPLITTING)
async def update(_self, sender, _recipient, _args):
    def worker():
        supervisor.update()

    if sender.nick in ADMINS:
        await curio.run_in_thread(worker)


@bot.on_command("!commit", 0)
async def commit(self, sender, recipient):
    info = (
        await subprocess.check_output(["git", "log", "-1", "--pretty=%ai\t%B"])
    ).decode("utf-8")
    update_time, commit_message = info.split("\t", 1)
    commit_summary = commit_message.splitlines()[0]

    await bot.send_privmsg(
        recipient,
        f"{sender.nick}: Updated at {update_time}: {commit_summary!r}",
    )


@bot.on_command("!buildstate", 0)
async def build_state(self, sender, recipient):
    state = await curio.run_in_thread(supervisor.build_state)
    await bot.send_privmsg(
        recipient, f"{sender.nick}: The build state is {state!r}"
    )


@bot.on_command("!reload", NO_SPLITTING)
async def bot_reload(_self, sender, _recipient, _args):
    def worker():
        supervisor.restart()

    if sender.nick in ADMINS:
        await curio.run_in_thread(worker)


def _add_canned_response(self, limiter, regexp, response):
    async def _canned_response(inner_self, _sender, recipient, _match):
        if "*" in limiter or recipient in limiter:
            await inner_self.send_privmsg(recipient, response)

    self.on_regexp(regexp)(_canned_response)


@bot.on_connect
async def add_canned_responses(self):
    canned_responses = self.state.get("canned_responses", {})
    for regexp, (limiter, response) in canned_responses.items():
        _add_canned_response(self, limiter, regexp, response)


@bot.on_command("!can", NO_SPLITTING)
async def canned_response(self, sender, recipient, args):
    try:
        limiter, regexp, response = args.split(" ", 2)
    except ValueError:
        await self.send_privmsg(
            recipient, f"{sender.nick}: !can LIMITER REGEXP RESP"
        )
        return

    try:
        re.compile(regexp)
    except re.error as e:
        await self.send_privmsg(
            recipient, f"{sender.nick}: Your RegExp is invalid ({e})"
        )
        return

    canned_responses = self.state.setdefault("canned_responses", {})
    canned_responses[regexp] = (limiter.split(","), response)
    _add_canned_response(self, limiter, regexp, response)
    await self.send_privmsg(
        recipient, f"{sender.nick}: Successfully canned your response."
    )


@bot.on_command("!uncan", 1)
async def uncan_response(self, _sender, recipient, regexp):
    canned_responses = self.state.get("canned_responses", {})

    if regexp in canned_responses:
        del canned_responses[regexp]

        compiled_regexp = re.compile(regexp)
        callbacks = self._regexp_callbacks[compiled_regexp]
        # You might ask "Why do you use a manual counter over enumerate?".
        # The answer is that reversed(enumerate(iterable)) does not work, and
        # enumerate(reversed(sequence)) doesn't work either.
        # We could convert the enumerate(iterable) return value
        # to a list or tuple, but that would bring all of the callbacks into
        # memory.
        # Keeping a manual counter is the most readable version and most memory
        # efficient.
        i = 0

        for callback in reversed(callbacks):
            try:
                name = callback.__code__.co_name
            except AttributeError:
                pass
            else:
                if name == "_canned_response":
                    del self._regexp_callbacks[compiled_regexp][i]
            i += 1

        await self.send_privmsg(recipient, f"Successfully removed {regexp!r}.")


@bot.on_command("!cans")
async def cans(self, sender, recipient, *_):
    canned_responses = self.state.get("canned_responses", {})

    await self.send_privmsg(recipient, f"{sender.nick}: Check your PMs!")
    for regexp, response in canned_responses.items():
        await self.send_privmsg(sender.nick, f"{regexp!r} -> {response!r}")
        await curio.sleep(1 / 10)  # 10 cans per second.


async def main():

    with supervisor:
        asks.init("curio")

        password = os.environ.get("IRC_PASSWORD")

        if len(sys.argv) > 1 and sys.argv[1] == "debug":
            nickname = os.environ["IRC_NICKNAME"]
            await bot.connect(
                nickname,
                "chat.freenode.net",
                sasl_password=password,
                enable_ssl=True,
            )

            await bot.join_channel("#8banana-bottest")
        else:
            await bot.connect(
                "pyhtonbot",
                "chat.freenode.net",
                sasl_password=password,
                enable_ssl=True,
            )

            await bot.join_channel("#8banana")
            await bot.join_channel("##learnpython")
            await bot.join_channel("#lpmc")
            await bot.join_channel("#learnprogramming")

        await bot.mainloop()


if __name__ == "__main__":
    curio.run(main)
