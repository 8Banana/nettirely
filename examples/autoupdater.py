# A simple script that allows you to update an IRC bot remotely without needing
# to manually restart it.

import atexit
import os
import subprocess
import sys
import threading

# This interval is 10 minutes because it's fun to see your updates come out
# when you want them to.
INTERVAL = int(1 * 60 * 10)  # seconds
update_condition = threading.Condition()

filepath = None


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



def _worker():
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

        with update_condition:
            update_condition.wait(INTERVAL)


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


def initialize():
    threading.Thread(target=_worker, daemon=True).start()
