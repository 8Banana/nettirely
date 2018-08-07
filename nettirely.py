import atexit
import base64
import collections
import inspect
import json
import os
import re

import curio


User = collections.namedtuple("User", ["nick", "user", "host"])
Server = collections.namedtuple("Server", ["name"])
Message = collections.namedtuple("Message", ["sender", "command", "args"])

ANY_ARGUMENTS = -1  # any amount of arguments, fully split
NO_SPLITTING = -2  # any amount of arguments, no splitting

ALWAYS_CALLBACK_PRIVMSG = True


def _create_callback_registration(key):
    def _inner(self, func):
        if not inspect.iscoroutinefunction(func):
            raise ValueError("You can only register coroutines!")
        self._message_callbacks.setdefault(key, []).append(func)
        return func
    return _inner


class IrcBot:
    """
    The main IrcBot class.

    You should instantiate this at the top of your bot,
    and use its decorators to register event handlers.

    Public instance attributes:
        nick: The nickname of the bot as a str.
        encoding: The encoding used to communicate with the server as a str.
        running: A boolean representing if the bot's mainloop is running.

        channel_users: A dict mapping each channel name to the users in it.

        state: A dictionary that is saved and loaded to a json file.
               Useful for keeping variables between runs of the bot.

        state_path: The path of the file to save the state to.
                    Defaults to the directory of the script + state.json
                    You can change this either on the class or on an instance.
    """

    state_path = os.path.join(os.path.dirname(__file__), "state.json")
    quit_reason = "Goodbye!"

    def __init__(self, encoding="utf-8", state_path=None):
        """
        Initializes an IrcBot instance.

        The only parameter for the initializer is encoding.
        The other parameters you would expect are taken in other methods.
        """

        if state_path is not None:
            self.state_path = state_path

        self.nick = None
        self.encoding = encoding
        self.running = True

        self._linebuffer = collections.deque()
        self._sock = None

        self.channel_users = {}

        try:
            with open(self.state_path) as f:
                self.state = json.load(f)
        except (ValueError, FileNotFoundError):
            self.state = {}

        atexit.register(self._on_exit)

        self._connection_callbacks = []
        self._disconnection_callbacks = []

        self._message_callbacks = {}
        self._command_callbacks = {}
        self._regexp_callbacks = {}

    def _on_exit(self):
        # We're in a weird place here, so I made the design decision to only
        # allow non-coroutines.
        # It would've been possible to also allow coroutines, however that
        # would involve creating a new curio Kernel.
        for callback in self._disconnection_callbacks:
            callback(self)

        self.save_state()

    def save_state(self):
        """
        Save the bot's state to a json file.

        This method is only safe if all the state is JSON encodeable.
        """
        with open(self.state_path + ".tmp", "w") as f:
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())

        os.rename(self.state_path + ".tmp", self.state_path)

    async def _send(self, *parts):
        data = " ".join(parts).encode(self.encoding) + b"\r\n"
        await self._sock.sendall(data)

    async def _send_in_chunks(self, cmd, data, chunk_length):
        while data:
            if len(data) < chunk_length:
                await self._send(cmd, data)
                return False
            elif len(data) == chunk_length:
                await self._send(cmd, data)
                return True
            else:  # len(data) > chunk_length
                chunk, data = data[:chunk_length], data[chunk_length:]
                await self._send(cmd, chunk)

    async def _recv_line(self, *, autoreply_to_ping=True, skip_empty_lines=True):
        if not self._linebuffer:
            data = bytearray()
            while not data.endswith(b"\r\n"):
                chunk = await self._sock.recv(4096)
                if chunk:
                    data += chunk
                else:
                    raise IOError("Server closed the connection!")

            lines = data.decode(self.encoding, errors='replace').split("\r\n")
            self._linebuffer.extend(lines)

        line = self._linebuffer.popleft()

        if autoreply_to_ping and line.startswith("PING"):
            await self._send(line.replace("PING", "PONG", 1))
            return await self._recv_line(autoreply_to_ping=True, skip_empty_lines=skip_empty_lines)
        elif skip_empty_lines and (not line):
            return await self._recv_line(autoreply_to_ping=autoreply_to_ping, skip_empty_lines=True)
        else:
            return line

    @staticmethod
    def _split_line(line):
        if line.startswith(":"):
            sender, command, *args = line.split(" ")
            sender = sender[1:]
            if "!" in sender:
                nick, sender = sender.split("!", 1)
                user, host = sender.split("@", 1)
                sender = User(nick, user, host)
            else:
                sender = Server(sender)
        else:
            sender = None
            command, *args = line.split(" ")
        for n, arg in enumerate(args):
            if arg.startswith(":"):
                temp = args[:n]
                temp.append(" ".join(args[n:])[1:])
                args = temp
                break
        return Message(sender, command, args)

    async def connect(self, nick, host, port=6667, *, sasl_username=None, sasl_password=None, sasl_mechanism="PLAIN",
                      enable_ssl=False):
        """
        Connects to an IRC server specified by host and port with a given nick.
        """

        self.nick = nick

        self._sock = await curio.open_connection(host, port, ssl=enable_ssl, server_hostname=host)

        username = "".join(c for c in self.nick if c.isalpha())

        # We need to track if we started capability negotiation to finish it.
        capability_negotation_started = False

        # Ask for SASL if we need it.
        if sasl_password is not None:
            capability_negotation_started = True
            await self._send("CAP", "REQ", "sasl")

        if port is None:
            port = 6697 if enable_ssl else 6667

        await self._send("NICK", self.nick)
        await self._send("USER", username, "0", "*", ":" + username)

        while True:
            msg = self._split_line(await self._recv_line())

            if msg.command == "CAP":
                subcommand = msg.args[1]

                if subcommand == "ACK":
                    acknowledged = set(msg.args[-1].split())

                    if "sasl" in acknowledged:
                        await self._send("AUTHENTICATE", sasl_mechanism)
                elif subcommand == "NAK":
                    rejected = set(msg.args[-1].split())

                    if "sasl" in rejected:
                        raise ValueError("The server does not support SASL.")
            elif msg.command == "AUTHENTICATE":
                if sasl_mechanism == "PLAIN":
                    if sasl_username is None:
                        query = f"\0{self.nick}\0{sasl_password}"
                    else:
                        query = f"{sasl_username}\0{self.nick}\0{sasl_password}"
                else:
                    raise ValueError(f"SASL mechanism {sasl_mechanism!r} is not supported.")

                b64_query = base64.b64encode(query.encode("utf-8")).decode("utf-8")

                await self._send_in_chunks("AUTHENTICATE", b64_query, chunk_length=400)
            elif msg.command == "900":  # RPL_LOGGEDIN
                if capability_negotation_started:
                    await self._send("CAP", "END")
            elif msg.command == "904":  # RPL_SASLFAILED
                raise ValueError("Failed to authenticate with SASL.")
            elif msg.command == "433":  # ERR_NICKNAMEINUSE
                raise ValueError(f"The nickname {self.nick!r} is already in use.")
            elif msg.command == "432":  # ERR_ERRONEUSNICKNAME
                raise ValueError(f"The nickname {self.nick!r} is erroneous.")
            elif msg.command == "001":  # RPL_WELCOME
                break

        async with curio.TaskGroup() as g:
            for callback in self._connection_callbacks:
                await g.spawn(callback(self))
            await g.join()

    async def join_channel(self, channel):
        await self._send("JOIN", channel)

    async def kick(self, channel, nickname, reason):
        await self._send("KICK", channel, nickname, ":" + reason)

    async def send_notice(self, recipient, text):
        await self._send("NOTICE", recipient, ":" + text)

    async def send_privmsg(self, recipient, text):
        await self._send("PRIVMSG", recipient, ":" + text)

    async def send_action(self, recipient, action):
        """
        Sends an action to a recipient.

        This is akin to doing "/me some action" on a regular IRC client.
        """

        await self._send("PRIVMSG", recipient,
                         ":\x01ACTION {}\x01".format(action))

    async def mainloop(self):
        """
        Handles keeping the connection alive and event handlers.
        """

        while self.running:
            line = await self._recv_line()
            msg = self._split_line(line)

            # The following block handles self.channel_users
            if msg.command == "353":  # RPL_NAMREPLY
                channel = msg.args[2]
                nicks = [nick.lstrip("@+")
                         for nick in msg.args[3].split()]
                self.channel_users.setdefault(channel, set()).update(nicks)
            elif msg.command == "JOIN":
                channel = msg.args[0]
                nick = msg.sender.nick
                self.channel_users.setdefault(channel, set()).add(nick)
            elif msg.command == "PART":
                channel = msg.args[0]
                nick = msg.sender.nick
                self.channel_users.setdefault(channel, set()).discard(nick)

            callbacks = self._message_callbacks.get(msg.command, ())
            async with curio.TaskGroup() as g:
                spawn_callbacks = True
                if msg.command == "PRIVMSG":
                    recipient = msg.args[0]
                    if recipient == self.nick:
                        recipient = msg.sender.nick

                    command, *args = msg.args[1].strip().split(" ")
                    cmd_callbacks = self._command_callbacks.get(command, ())
                    for callback, arg_amount in cmd_callbacks:
                        if arg_amount == NO_SPLITTING:
                            spawn_callbacks = False
                            coro = callback(self, msg.sender, recipient,
                                            " ".join(args))
                            await g.spawn(coro)
                        elif arg_amount == ANY_ARGUMENTS or \
                                len(args) == arg_amount:
                            spawn_callbacks = False
                            coro = callback(self, msg.sender, recipient,
                                            *args)
                            await g.spawn(coro)

                    for regexp, regexp_callbacks in self._regexp_callbacks.items():
                        for match in regexp.finditer(msg.args[1]):
                            spawn_callbacks = False

                            for callback in regexp_callbacks:
                                coro = callback(self, msg.sender, recipient,
                                                match)
                                await g.spawn(coro)

                if ALWAYS_CALLBACK_PRIVMSG or spawn_callbacks:
                    # Sometimes we don't want to spawn the PRIVMSG callbacks if
                    # this is a command.
                    for callback in callbacks:
                        await g.spawn(callback(self, msg.sender, *msg.args))
                await g.join()

        await self._send("QUIT", ":" + self.quit_reason)

    def on_connect(self, func):
        if not inspect.iscoroutinefunction(func):
            raise ValueError("You can only register coroutines!")
        self._connection_callbacks.append(func)

    def on_disconnect(self, func):
        # Registers a coroutine to be ran right before exit.
        # This is so you can modify your state to be JSON-compliant.
        if inspect.iscoroutinefunction(func):
            raise ValueError("You can only register non-coroutines!")
        self._disconnection_callbacks.append(func)

    on_privmsg = _create_callback_registration("PRIVMSG")
    on_join = _create_callback_registration("JOIN")
    on_part = _create_callback_registration("PART")
    on_quit = _create_callback_registration("QUIT")

    def on_command(self, command, arg_amount=ANY_ARGUMENTS):
        """
        Creates a decorator that registers a command handler.

        The argument command must include the prefix.

        The command handler takes as arguments:
            1. The bot instance
            2. The command sender.
            3. The command recipient, usually a channel.
            4. Any arguments that came with the command,
               split depending on the arg_amount argument.

        As an example, to register a command that looks like this:
            !slap nickname

        You'd write something like this:
            @bot.on_command("!slap", arg_amount=1)
            def slap(self, sender, recipient, slappee):
                ...
        """

        def _inner(func):
            if not inspect.iscoroutinefunction(func):
                raise ValueError("You can only register coroutines!")
            self._command_callbacks.setdefault(command, [])\
                                   .append((func, arg_amount))
            return func
        return _inner

    def on_regexp(self, regexp):
        """
        Creates a decorator that registers a regexp command handler.

        The regexp command handler takes as arguments:
            1. The bot instance
            2. The command sender
            3. The command recipient, usually a channel
            4. The match object, for any groups you might wanna extract.

        The regexp is searched, not just matched.
        Your handler might get called multiple times per message,
        depending on the amount of matches.
        """

        regexp = re.compile(regexp)

        def _inner(func):
            if not inspect.iscoroutinefunction(func):
                raise ValueError("You can only register coroutines!")
            self._regexp_callbacks.setdefault(regexp, [])\
                                  .append(func)
            return func
        return _inner
