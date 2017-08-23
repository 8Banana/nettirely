#!/usr/bin/env python3
# A simple script that allows you to update an IRC bot remotely without needing
# to manually restart it.

import atexit
import inspect
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
    process = subprocess.run(args,
                             stdout=subprocess.PIPE)
    assert process.returncode == 0
    return process.stdout.decode("ascii").strip()


def _worker():
    remote = "origin"
    branch = _get_output(["git", "symbolic-ref", "--short", "HEAD"])
    commit_hash = _get_output(["git", "rev-parse", "HEAD"])

    while True:
        command = subprocess.run(["git", "pull", remote, branch],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

        if command.returncode == 0:
            new_commit_hash = _get_output(["git", "rev-parse", "HEAD"])

            if new_commit_hash != commit_hash:
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

    os.execlp(sys.executable, sys.executable, filepath)


def initialize():
    # TODO: Not use globals.
    global filepath

    # Initialize the auto-updater. Must be called in the main script.
    parent_globals = inspect.currentframe().f_back.f_globals
    assert parent_globals["__name__"] == "__main__"
    filepath = parent_globals["__file__"]
    threading.Thread(target=_worker, daemon=True).start()
