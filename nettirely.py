import atexit
import collections
import logging
import inspect
import json
import os
import re
from base64 import b64encode

import curio


User = collections.namedtuple("User", ["nick", "user", "host"])
Server = collections.namedtuple("Server", ["name"])
Message = collections.namedtuple("Message", ["sender", "command", "args"])

ANY_ARGUMENTS = -1  # any amount of arguments, fully split
NO_SPLITTING = -2  # any amount of arguments, no splitting


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

    logger = logging.getLogger(__name__)

    def __init__(self, encoding="utf-8", state_path=None):
        """
        Initializes an IrcBot instance.

        The only parameter for the initializer is encoding.
        The other parameters you would expect are taken in other methods.
        """

        if state_path is not None:
            self.state_path = state_path
        else:
            self.state_path = os.path.join(
                os.path.dirname(__file__), "state.json"
            )

        self.nick = None
        self.encoding = encoding

        self.channel_users = {}

        self._running = True
        self._linebuffer = collections.deque()
        self._sock = None

        try:
            with open(self.state_path) as f:
                self.state = json.load(f)
        except (ValueError, FileNotFoundError):
            self.state = {}

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
            self.logger.debug(
                "Calling disconnection callback %s ...", callback
            )
            callback(self)

        self.save_state()

    def save_state(self):
        """
        Save the bot's state to a json file.

        This method is only safe if all the state is JSON encodeable.
        """
        self.logger.debug("Writing the state to a swap file ...")
        with open(self.state_path + ".tmp", "w") as f:
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())

        self.logger.info("Writing the state file ...")
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

    async def _recv_line(
        self, *, autoreply_to_ping=True, skip_empty_lines=True
    ):
        if not self._linebuffer:
            data = bytearray()
            while not data.endswith(b"\r\n"):
                chunk = await self._sock.recv(4096)
                if chunk:
                    data += chunk
                else:
                    raise IOError("Server closed the connection!")

            lines = data.decode(self.encoding, errors="replace").split("\r\n")
            self._linebuffer.extend(lines)

        line = self._linebuffer.popleft()

        if autoreply_to_ping and line.startswith("PING"):
            await self._send(line.replace("PING", "PONG", 1))
            return await self._recv_line(
                autoreply_to_ping=True, skip_empty_lines=skip_empty_lines
            )
        elif skip_empty_lines and (not line):
            return await self._recv_line(
                autoreply_to_ping=autoreply_to_ping, skip_empty_lines=True
            )
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

    async def connect(
        self,
        nick,
        host,
        port=None,
        *,
        sasl_username=None,
        sasl_password=None,
        sasl_mechanism="PLAIN",
        enable_ssl=False,
    ):
        """
        Connects to an IRC server.

        The arguments `host` and `port` specify the server`s hostname and port.
        The argument `host` is also passed as `server_hostname` to `curio.open_connection`.

        The argument `nick` is used both as nickname and as username/realname.
        Nota bene: Any non-alphanumeric characters are filtered from `nick`
        to construct the username/realname.

        The arguments `sasl_username`, `sasl_password`, and `sasl_mechanism`
        are used to specify settings for SASL. It is usually enough to only
        set `sasl_password`, seeing as the only mechanism supported is PLAIN
        and the username only matters if you are part of a NickServ GROUP.

        The argument `enable_ssl` specifies if TLS is used for connection with the server.
        If the argument `port` is not given, it also modifies its default value.
        """

        self.nick = nick

        if port is None:
            port = 6697 if enable_ssl else 6667

        self.logger.info("Opening connection to %s:%s ...", host, port)
        self._sock = await curio.open_connection(
            host, port, ssl=enable_ssl, server_hostname=host
        )
        self.logger.info("Opened connection to %s:%s", host, port)

        # We need to track if we started capability negotiation to finish it.
        capability_negotation_started = False

        # Ask for SASL if we need it.
        if sasl_password is not None:
            self.logger.info("Starting negotation for SASL ...")
            capability_negotation_started = True
            await self._send("CAP", "REQ", "sasl")

        # NOTE: If you are adding more things here that require capabilities,
        # use a separate "CAP REQ" message for each capability, unless it
        # makes sense to group them together.

        self.logger.info("Sending authentication ... ")
        username = "".join(c for c in self.nick if c.isalpha())
        await self._send("NICK", self.nick)
        await self._send("USER", username, "0", "*", ":" + username)

        while True:
            msg = self._split_line(await self._recv_line())

            if msg.command == "CAP":
                subcommand = msg.args[1]

                if subcommand == "ACK":
                    acknowledged = set(msg.args[-1].split())

                    if "sasl" in acknowledged:
                        self.logger.info("SASL was acknowleged.")
                        await self._send("AUTHENTICATE", sasl_mechanism)
                elif subcommand == "NAK":
                    rejected = set(msg.args[-1].split())

                    if "sasl" in rejected:
                        self.logger.info("SASL was rejected.")
                        raise ValueError("The server does not support SASL.")
            elif msg.command == "AUTHENTICATE":
                if sasl_mechanism == "PLAIN":
                    if sasl_username is None:
                        query = f"\0{self.nick}\0{sasl_password}"
                    else:
                        query = (
                            f"{sasl_username}\0{self.nick}\0{sasl_password}"
                        )
                else:
                    raise ValueError(
                        f"SASL mechanism {sasl_mechanism!r} is not supported."
                    )

                b64_query = b64encode(query.encode("utf-8")).decode("utf-8")

                await self._send_in_chunks(
                    "AUTHENTICATE", b64_query, chunk_length=400
                )
            elif msg.command == "900":  # RPL_LOGGEDIN
                self.logger.info("Logged in with SASL.")
                if capability_negotation_started:
                    await self._send("CAP", "END")
            elif msg.command == "904":  # RPL_SASLFAILED
                self.logger.exception("Failed to log in with SASL.")
                raise ValueError("Failed to authenticate with SASL.")
            elif msg.command == "433":  # ERR_NICKNAMEINUSE
                self.logger.exception(
                    "Nickname %r is already in use.", self.nick
                )
                raise ValueError(
                    f"The nickname {self.nick!r} is already in use."
                )
            elif msg.command == "432":  # ERR_ERRONEUSNICKNAME
                self.logger.critical("Nickname %r is erroneous.", self.nick)
                raise ValueError(f"The nickname {self.nick!r} is erroneous.")
            elif msg.command == "001":  # RPL_WELCOME
                self.logger.info("Recieved welcome.")
                break

        async with curio.TaskGroup() as g:
            for callback in self._connection_callbacks:
                self.logger.debug(
                    "Spawning connection callback %r ...", callback
                )
                await g.spawn(callback(self))
            self.logger.debug("Waiting on connection callbacks ... ")
            await g.join()
            self.logger.debug("Everything went fine.")

        # We should only register disconnection callbacks if connection
        # actually happens.
        self.logger.debug("Registering exit callbacks ...")
        atexit.register(self._on_exit)

    async def join_channel(self, channel):
        """
        Join `channel`.
        """
        self.logger.info("Joining %r ...", channel)
        await self._send("JOIN", channel)

    async def kick(self, channel, nickname, reason):
        """
        Kick `nickname` from `channel` for `reason`.
        """
        self.logger.info(
            "Kicking %r from %r because %r ...", nickname, channel, reason
        )
        await self._send("KICK", channel, nickname, ":" + reason)

    async def send_notice(self, recipient, text):
        """
        Send a notice `text` to `recipient`.

        Nota bene: If you send a notice to a channel whose text is prefixed with
        "[{channel}] " most clients will display it as a Channel notice.
        """
        self.logger.debug("Sending notice %r to %r ...", text, recipient)
        await self._send("NOTICE", recipient, ":" + text)

    async def send_privmsg(self, recipient, text):
        """
        Send a privmsg `text` to `recipient`.
        """
        self.logger.debug("Sending privmsg %r to %r ...", text, recipient)
        await self._send("PRIVMSG", recipient, ":" + text)

    async def send_action(self, recipient, action):
        """
        Sends an action `action` to `recipient`.

        This is the same as writing "/me some action" on a regular IRC client.
        """

        self.logger.debug("Sending action %r to %r ...", action, recipient)
        await self._send(
            "PRIVMSG", recipient, ":\x01ACTION {}\x01".format(action)
        )

    async def mainloop(self):
        """
        Handles keeping the connection alive and dispatching event handlers.
        """

        while self._running:
            msg = self._split_line(await self._recv_line())
            self.logger.debug("Recieved message %r", msg)

            # Keep track of who's in each channel.
            if msg.command == "353":  # RPL_NAMREPLY
                channel = msg.args[2]
                nicks = [nick.lstrip("@+") for nick in msg.args[3].split()]
                self.channel_users.setdefault(channel, set()).update(nicks)
                self.logger.info(
                    "Users in %r: %r", channel, self.channel_users[channel]
                )
            elif msg.command == "JOIN":
                channel = msg.args[0]
                nick = msg.sender.nick
                self.channel_users.setdefault(channel, set()).add(nick)
                self.logger.info("%r joined %r", nick, channel)
            elif msg.command == "PART":
                channel = msg.args[0]
                nick = msg.sender.nick
                self.channel_users.setdefault(channel, set()).discard(nick)
                self.logger.info("%r left %r", nick, channel)

            async with curio.TaskGroup() as g:
                if msg.command == "PRIVMSG":
                    recipient = msg.args[0]
                    if recipient == self.nick:
                        recipient = msg.sender.nick

                    # Command callbacks. (e.g. !update, !commit, !parrot, ...)
                    command, *args = msg.args[1].strip().split(" ")
                    cmd_callbacks = self._command_callbacks.get(command, ())
                    for callback, arg_amount in cmd_callbacks:
                        if arg_amount == NO_SPLITTING:
                            self.logger.debug(
                                "Spawning command callback "
                                "%s(self, %r, %r, %r) for command %r",
                                callback.__name__,
                                msg.sender,
                                recipient,
                                " ".join(args),
                                command,
                            )
                            await g.spawn(
                                callback(
                                    self, msg.sender, recipient, " ".join(args)
                                )
                            )
                        elif (
                            arg_amount == ANY_ARGUMENTS
                            or len(args) == arg_amount
                        ):
                            self.logger.debug(
                                "Spawning command callback "
                                "%s(self, %r, %r, *%r) for command %r",
                                callback.__name__,
                                msg.sender,
                                recipient,
                                args,
                                command,
                            )
                            await g.spawn(
                                callback(self, msg.sender, recipient, *args)
                            )

                    # RegExp callbacks.
                    for (
                        regexp,
                        regexp_callbacks,
                    ) in self._regexp_callbacks.items():
                        for match in regexp.finditer(msg.args[1]):

                            for callback in regexp_callbacks:
                                self.logger.debug(
                                    "Spawning RegExp callback "
                                    "%s(self, %r, %r, %r) for RegExp %r",
                                    callback.__name__,
                                    msg.sender,
                                    recipient,
                                    match,
                                    regexp,
                                )

                                await g.spawn(
                                    callback(
                                        self, msg.sender, recipient, match
                                    )
                                )

                # Message callbacks. (e.g. JOIN, PART, PRIVMSG, ...)
                message_callbacks = self._message_callbacks.get(
                    msg.command, ()
                )

                for callback in message_callbacks:
                    self.logger.debug(
                        "Spawning message callback "
                        "%s(self, %r, %r, *%r) for message %r",
                        callback.__name__,
                        msg.sender,
                        recipient,
                        msg.args,
                        msg.command,
                    )

                    await g.spawn(callback(self, msg.sender, *msg.args))

    async def quit(self, reason="Goodbye!"):
        self.logger.info("Quitting because %r", reason)
        self._running = False
        await self._send("QUIT", ":" + reason)

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
        Create a decorator that registers a command handler.

        If you want a prefix on your command, such as in "!update", you must
        add one manually to the `command` argument.

        The command handler will be given as arguments:
            1. The IrcBot instance.

            2. The command Sender.

            3. The recipient that should be used to reply to the command.
               If the command was sent to a channel the bot is in, this
               argment will be that channel.
               If the  command was sent as a private message to the bot, this
               argument will be the sender's nickname.

            4. Any arguments that came with the command,
               split depending on the `arg_amount` argument.

        e.g: To register a command that looks like this:
            !slap nickname

        You'd write something like this:
            @bot.on_command("!slap", arg_amount=1)
            def slap(self, sender, recipient, slappee):
                ...
        """

        def _inner(func):
            if not inspect.iscoroutinefunction(func):
                raise ValueError("You can only register coroutines!")
            self.logger.debug(
                "Registered function %r (args: %r) for command %r",
                func,
                arg_amount,
                command,
            )
            self._command_callbacks.setdefault(command, []).append(
                (func, arg_amount)
            )
            return func

        return _inner

    def on_regexp(self, regexp):
        """
        Create a decorator that registers a RegExp command handler.

        The regexp command handler takes as arguments:
            1. The IrcBot instance.

            2. The command Sender.

            3. The recipient that should be used to reply to the message.
               If the command was sent to a channel the bot is in, this
               argment will be that channel.
               If the  command was sent as a private message to the bot, this
               argument will be the sender's nickname.

            4. The RegExp match object.

        The regexp is `re.search`ed, not `re.match`ed.

        Your handler will be called once per match per message.
        This means that your handler may be called more than
        once per message, if the RegExp matches two or more times.
        """

        regexp = re.compile(regexp)

        def _inner(func):
            if not inspect.iscoroutinefunction(func):
                raise ValueError("You can only register coroutines!")
            self.logger.debug(
                "Registered function %r for RegExp %r", func, regexp
            )
            self._regexp_callbacks.setdefault(regexp, []).append(func)
            return func

        return _inner
