#!/usr/bin/env python3
import anyio
import asks
import bs4

from supervisor import Supervisor
from nettirely import IrcBot

bot = IrcBot(state_path="factoid_state.json")
supervisor = Supervisor("8Banana/nettirely")


async def create_termbin(contents):
    if isinstance(contents, str):
        contents = contents.encode("utf-8")

    socket = await anyio.connect_tcp("termbin.com", 9999)

    async with socket:
        await socket.send_all(contents)
        url = await socket.receive_some(4096)
        return url.decode("utf-8").strip()


@bot.on_regexp(r"^\$(\S+)(?:\s*(.+))?")
async def factoid_handler(self, sender, recipient, match):
    factoid = match.group(1)
    args = match.group(2).split() if match.group(2) else []
    nick = sender.nick
    admins = bot.state.setdefault("admins", ["tycoon177", "darkf", "__Myst__"])
    lastfm_api_key = bot.state.setdefault("lastfm_api_key", None)
    factoids = bot.state.setdefault("factoids", {})

    if factoid == "defact" and len(args) >= 1:
        factoids[args[0]] = " ".join(args[1:])
        await self.send_privmsg(recipient, f"{nick}: Defined factoid {args[0]!r}")
        await anyio.run_in_thread(self.save_state)
    elif factoid == "delfact" and len(args) >= 1:
        factoids = bot.state.setdefault("factoids", {})
        if factoids.pop(args[0], None) is not None:
            await self.send_privmsg(recipient, f"{nick}: Removed factoid {args[0]!r}")
            await anyio.run_in_thread(self.save_state)
        else:
            await self.send_privmsg(recipient, f"{nick}: No such factoid exists")
    elif factoid == "defadmin" and nick in admins and len(args) >= 1:
        for user in args:
            if user not in admins:
                admins.append(user)
        users = ", ".join(args)
        await self.send_privmsg(recipient, f"{nick}: Added admins {users}")
        await anyio.run_in_thread(self.save_state)
    elif factoid == "deladmin" and nick in admins and len(args) >= 1:
        for user in args:
            try:
                admins.remove(user)
            except ValueError:
                pass
        users = ", ".join(args)
        await self.send_privmsg(recipient, f"{nick}: Removed admins {users}")
        await anyio.run_in_thread(self.save_state)
    elif factoid == "setapikey" and nick in admins and len(args) >= 1:
        bot.state["lastfm_api_key"] = args[0]
        await self.send_privmsg(recipient, f"{nick}: lastfm api key updated")
        await anyio.run_in_thread(self.save_state)
    elif factoid == "factoids":
        url = await create_termbin(" ".join(factoids))
        await self.send_privmsg(recipient, url)
    elif factoid == "admins":
        url = await create_termbin(" ".join(admins))
        await self.send_privmsg(recipient, url)
    elif factoid == "at" and len(args) >= 2:
        if args[1] in factoids:
            await self.send_privmsg(recipient, f"{args[0]}: {factoids[args[1]]}")
    elif factoid == "join" and len(args) >= 1 and sender.nick in admins:
        await self.join_channel(args[0])
    elif factoid == "quit" and sender.nick in admins:
        await self.quit()
    elif factoid == "np" and lastfm_api_key is not None:
        lastfm_user = args[0] if len(args) >= 1 else sender.nick

        resp = await asks.get(
            "http://ws.audioscrobbler.com/2.0/",
            params={
                "method": "user.getrecenttracks",
                "user": lastfm_user,
                "api_key": lastfm_api_key,
                "limit": "1",
            },
        )

        soup = bs4.BeautifulSoup(resp.text, "xml")

        try:
            artist = soup.artist.text
            album = soup.album.text
            title = soup.find("name").text
        except AttributeError:
            # This happens when there are no tracks returned for some reason.
            return

        msg = f"{sender.nick}: {lastfm_user} is listening to {artist} - {title}"
        if album:
            msg += f" (from {album})"
        await self.send_privmsg(recipient, msg)
    elif factoid == "update" and nick in admins:
        supervisor.update()
    else:
        factoids = bot.state.get("factoids", {})
        if factoid in factoids:
            await self.send_privmsg(recipient, factoids[factoid])


async def main():
    with supervisor:
        await bot.connect("factoid_bot8", "chat.freenode.net")
        await bot.join_channel("#8banana-bottest")

        await bot.mainloop()


if __name__ == "__main__":
    anyio.run(main)
