#!/usr/bin/env python3
"""
The 8Banana team's own bot.
We actually use this one on the #8Banana IRC channel :)
"""

import collections
import datetime
import random
import re
import sys
import time
import urllib.parse

import asks
import curio
from curio import subprocess

import autoupdater
from nettirely import IrcBot, NO_SPLITTING

TIME_AMOUNTS = ("second", "minute", "hour")
SLAP_TEMPLATE = "slaps {slappee} around a bit with {fish}"

FISH = (
    "asyncio", "multiprocessing", "twisted", "django", "pathlib",
    "python 2.7", "a daemon thread", "unittest", "logging",
    "urllib.request.HTTPPasswordMgrWithDefaultRealm", "javascript",
)
ADMINS = {"__Myst__", "theelous3", "Akuli", "Zaab1t"}

bot = IrcBot(state_path="pyhtonbot_state.json")


@bot.on_command(">>>", NO_SPLITTING)
async def annoy_raylu(self, _, recipient, text):
    if recipient == self.nick:
        return
    await self.send_privmsg(recipient, "!py3 " + text)


@bot.on_command("!slap", 1)
async def slap(self, _, recipient, slappee):
    fish = random.choice(FISH)
    await self.send_action(recipient, SLAP_TEMPLATE.format(slappee=slappee,
                                                           fish=fish))


@bot.on_connect
async def initialize_logs(self):
    logs = collections.defaultdict(lambda: collections.deque(maxlen=500))

    if "logs" in self.state:
        logs.update({k: collections.deque(v, maxlen=500)
                     for k, v in self.state["logs"].items()})

    self.state["logs"] = logs


@bot.on_disconnect
def save_logs(self):
    self.state["logs"] = \
        {k: list(v) for k, v in self.state.get("logs", {}).items()}


async def upload_log(lines):
    resp = await asks.post("https://theelous3.net/irc_log",
                           data="\n".join(lines))
    return resp.text


@bot.on_join
async def append_join_to_log(self, sender, channel):
    logs = self.state["logs"]
    now = datetime.datetime.now().strftime("%X")
    logs[channel].append(f'[{now}] {sender.nick} joined {channel}')


@bot.on_part
async def append_part_to_log(self, sender, channel, reason=None):
    if reason is None:
        reason = "No reason."

    logs = self.state["logs"]
    now = datetime.datetime.now().strftime("%X")
    logs[channel].append(f'[{now}] {sender.nick} parted {channel} ({reason})')


@bot.on_quit
async def append_quit_to_log(self, sender, reason=None):
    if reason is None:
        reason = "No reason."

    now = datetime.datetime.now().strftime("%X")

    # Can we know what channels to show this in?
    msg = f'[{now}] {sender.nick} quit ({reason})'

    logs = self.state["logs"]

    for channel, users in self.channel_users.items():
        if sender.nick in users:
            logs[channel].append(msg)


@bot.on_privmsg
async def append_privmsg_to_log(self, sender, channel, message):
    now = datetime.datetime.now().strftime("%X")
    logs = self.state["logs"]
    logs[channel].append(f'[{now}] <{sender.nick}> {message}')


@bot.on_command("!log", 0)
async def send_log(self, sender, channel):

    msg = f"{sender.nick}: Uploading logs, this might take a second..."
    await self.send_privmsg(channel, msg)
    logs = self.state["logs"]
    result = await upload_log(logs[channel])
    await self.send_privmsg(channel, f"{sender.nick}: {result}")


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

        await self.send_privmsg(channel,
                                f"{sender.nick}: {user} was last seen {when}.")
    else:
        await self.send_privmsg(channel,
                                f"{sender.nick}: I've never seen {user}.")


def _make_url(domain, what2google):
    # example response: 'http://www.lmfgtfy.com/?q=wolo+wolo'
    params = urllib.parse.urlencode({'q': what2google})
    return "http://www.%s/?%s" % (domain, params)


async def _respond(self, recipient, domain, text):
    if recipient == self.nick:
        return

    try:
        target, what2google = text.split(maxsplit=1)
    except ValueError:
        command = "fgoogle" if domain == "lmfgtfy.com" else "google"
        await self.send_privmsg(recipient,
                                "Usage: !%s nick what2google" % command)
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
        await self.send_privmsg(recipient,
                                f"{sender.nick}: You will recieve logs automatically.")
    elif argument == "off":
        if sender.nick in self.state["autologgers"]:
            self.state["autologgers"].remove(sender.nick)
        await self.send_privmsg(recipient,
                                f"{sender.nick}: You will not recieve logs automatically anymore.")
    else:
        await self.send_privmsg(recipient, f"{sender.nick}: USAGE: !autolog on/off")


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
        with autoupdater.update_condition:
            autoupdater.update_condition.notify_all()

    if sender.nick in ADMINS:
        await curio.run_in_thread(worker)

@bot.on_command("!reload", NO_SPLITTING)
async def bot_reload(_self, sender, _recipient, _args):
    def worker():
        autoupdater.restart()

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
        await self.send_privmsg(recipient,
                                f"{sender.nick}: !can LIMITER REGEXP RESP")
        return

    try:
        re.compile(regexp)
    except re.error as e:
        await self.send_privmsg(recipient,
                                f"{sender.nick}: Your RegExp is invalid ({e})")
        return

    canned_responses = self.state.setdefault("canned_responses", {})
    canned_responses[regexp] = (limiter.split(","), response)
    _add_canned_response(self, limiter, regexp, response)
    await self.send_privmsg(recipient,
                            f"{sender.nick}: Successfully canned your response.")


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

        await self.send_privmsg(recipient,
                                f"Successfully removed {regexp!r}.")


@bot.on_command("!cans")
async def cans(self, sender, recipient, *_):
    canned_responses = self.state.get("canned_responses", {})

    await self.send_privmsg(recipient,
                            f"{sender.nick}: Check your PMs!")
    for regexp, response in canned_responses.items():
        await self.send_privmsg(sender.nick,
                                f"{regexp!r} -> {response!r}")
        await curio.sleep(1 / 10)  # 10 cans per second.


async def main():
    asks.init("curio")
    autoupdater.initialize()

    if len(sys.argv) > 1 and sys.argv[1] == "debug":
        await bot.connect("pyhtonbot2", "chat.freenode.net")
        await bot.join_channel("#8banana-bottest")
    else:
        await bot.connect("pyhtonbot", "chat.freenode.net")
        await bot.join_channel("#8banana")
        await bot.join_channel("##learnpython")
        await bot.join_channel("#lpmc")
        await bot.join_channel("#learnprogramming")

    # this only sent to #8banana
    info = (await subprocess.check_output(
        ['git', 'log', '-1', '--pretty=%ai\t%B'])).decode('utf-8')
    update_time, commit_message = info.split("\t", 1)
    commit_summary = commit_message.splitlines()[0]

    await bot.send_privmsg("#8banana",
                           f"Updated at {update_time}: {commit_summary!r}")

    while True:
        try:
            await bot.mainloop()
        except OSError:
            autoupdater.restart()

if __name__ == "__main__":
    try:
        curio.run(main)
    except KeyboardInterrupt:
        pass
