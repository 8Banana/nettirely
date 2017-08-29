#!/usr/bin/env python3
import traceback

import asks
import bs4
import curio

import autoupdater
from nettirely import IrcBot

# Change this to your API key if you want the $np functionality.
LASTFM_API_KEY = None
ADMINS = {"darkf", "__Myst__"}

bot = IrcBot(state_path="factoid_state.json")


@bot.on_regexp(r"^\$(\S+)(?:\s*(.+))?")
async def factoid_handler(self, sender, recipient, match):
    factoid = match.group(1)
    args = match.group(2).split() if match.group(2) else []
    nick = sender.nick

    if factoid == "defact" and len(args) >= 1:
        factoids = bot.state.setdefault("factoids", {})
        factoids[args[0]] = " ".join(args[1:])
        await self.send_privmsg(recipient,
                                f"{nick}: Defined factoid {args[0]!r}")
    elif factoid == "factoids":
        await self.send_privmsg(recipient,
                                " ".join(bot.state.get(factoid, {})))
    elif factoid == "at" and len(args) >= 2:
        factoids = bot.state.get("factoids", {})
        if args[1] in factoids:
            await self.send_privmsg(recipient,
                                    f"{args[0]}: {factoids[args[1]]}")
    elif factoid == "join" and len(args) >= 1 and sender.nick in ADMINS:
        await self.join_channel(args[0])
    elif factoid == "quit" and sender.nick in ADMINS:
        self.running = False
    elif factoid == "np" and len(args) >= 1 and LASTFM_API_KEY is not None:
        resp = await asks.get("http://ws.audioscrobbler.com/2.0/", params={
            "method": "user.getrecenttracks",
            "user": args[0],
            "api_key": LASTFM_API_KEY,
            "limit": "1",
        })

        soup = bs4.BeautifulSoup(resp.text, "xml")

        try:
            artist = soup.artist.text
            album = soup.album.text
            title = soup.find("name").text
        except AttributeError:
            # This happens when there are no tracks returned for some reason.
            return

        msg = f"{sender.nick}: {args[0]} is listening to {artist} - {title}"
        if album:
            msg += f" (from {album})"
        await self.send_privmsg(recipient, msg)
    else:
        factoids = bot.state.get("factoids", {})
        if factoid in factoids:
            await self.send_privmsg(recipient, factoids[factoid])


async def main():
    asks.init("curio")
    autoupdater.initialize()

    await bot.connect("factoid_bot8", "chat.freenode.net")
    await bot.join_channel("#8banana-bottest")

    while True:
        try:
            await bot.mainloop()
        except OSError:
            traceback.print_exc()
            autoupdater.restart()


if __name__ == "__main__":
    try:
        curio.run(main)
    except KeyboardInterrupt:
        pass
