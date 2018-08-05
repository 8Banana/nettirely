# A simple script that allows you to update an IRC bot remotely without needing
# to manually restart it.

import atexit
import os
import subprocess
import sys
import threading

INTERVAL = 1 * 60 * 10  # 10 minutes

_force_update_event = threading.Event()


def _get_output(args):
    process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if process.returncode == 0:
        return process.stdout.decode("ascii").strip()
    else:
        pid = os.getpid()

        with open("stdout.%d.log" % pid, "wb") as f:
            f.write(process.stdout)

        with open("stderr.%d.log" % pid, "wb") as f:
            f.write(process.stderr)

        raise RuntimeError(("There was an error while running the command %r."
                           "You can find stdout & stderr in the current working directory. (PID %d)") % (args, pid))


def restart():
    if hasattr(atexit, "_run_exitfuncs"):
        # We're about to leave in a way that's not expected by
        # Python.
        # This means that some things, including atexit callbacks,
        # won't be run.
        # We want them to run because ircbot.py relies on them, so
        # this is our kind-of CPython hack.
        atexit._run_exitfuncs()

    os.execvp(sys.executable, [sys.executable] + sys.argv)


def update():
    _force_update_event.set()


def initialize():
    def check_for_updates():
        remote = "origin"
        branch = _get_output(["git", "symbolic-ref", "--short", "HEAD"])
        current_commit_hash = _get_output(["git", "rev-parse", "HEAD"])

        while True:
            command = subprocess.run(["git", "pull", remote, branch],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

            if command.returncode == 0:
                new_commit_hash = _get_output(["git", "rev-parse", "HEAD"])

                if new_commit_hash != current_commit_hash:
                    restart()

            # This line sleeps until one of the following two conditions:
            #  1. INTERVAL seconds pass.
            #  2. Somebody sets _force_update_event.
            _force_update_event.wait(INTERVAL)
            _force_update_event.clear()

    threading.Thread(target=check_for_updates, daemon=True).start()
