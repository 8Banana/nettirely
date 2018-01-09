# nettirely

`nettirely` is the only IRC bot library for Python 3.6 usable by humans.

You must use Python3.6 due to the usage of `async`/`await` and f-strings.

Its API is simple to understand and usable, and will get your bot up to speed
in a matter of minutes.

Here's the power of nettirely:

```python
import curio

from nettirely import IrcBot, NO_SPLITTING

bot = IrcBot()


@bot.on_command("!echo", NO_SPLITTING)
def echo(self, sender, recipient, text):
    await self.send_privmsg(recipient, text)

async def main():
    await bot.connect("nettibot", "chat.freenode.net", 6667)
    await bot.join_channel("#8banana")
    await bot.mainloop()


if __name__ == "__main__":
    curio.run(main)
```

In just 20 lines of readable code, you've made an IRC bot which supports a
command.

You can also very easily combine `nettirely` with any library that supports
curio.  
For example, you can interact with an HTTP REST API using the [asks library](https://github.com/theelous3/asks)!

# Usage

The core of `nettirely` is the IrcBot class.
All of its methods are throughly documented, or are pretty much
self-explanatory.
You can look at the example given above or at [pyhtonbot](https://github.com/8Banana/nettirely/blob/master/examples/pyhtonbot.py) to see some examples of nettirely's power.

# Reasoning

IRC bots are useful to make repetitive tasks easy, extend IRC or just provide
fun games.

In the past, you either had to read through the IRC RFC yourself or use a
clunky library to write an IRC bot.

`nettirely` allows you to write a bot without worrying about PINGs, PONGs or
PANGs. It allows you to just focus on the important parts of your bot.

# Features It Has

1. Ability to do multiple actions on a PRIVMSG, JOIN, PART, or QUIT.
2. Ability to easily write commands with any amount of arguments, even
   infinite!
3. Compatibility with any existing curio library.
4. And more!

# Features It Will Have

1. Ability to do an action on *any* kind of IRC message.
2. NickServ and SASL support.
3. Whatever we think of!

# Footnote about the examples

The examples provided are actual fully functional bots utilized by users.
Most of them come with an autoupdater script that pulls from the repo to
update.
If you wish to actually utilize them, you should install the `nettirely` module in
editable mode.
This can be done via by running `python3 -m pip install --user -e .` in the
main directory of the repo, or `python3 setup.py develop`.
