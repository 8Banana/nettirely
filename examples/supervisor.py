# A simple script that allows you to update an IRC bot remotely without needing
# to manually restart it.

import atexit
import os
import urllib.parse
import logging
import traceback
import subprocess
import sys
import threading

import requests

TEN_MINUTES = 1 * 60 * 10

logger = logging.getLogger(__name__)


def _get_output(args):
    process = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    if process.returncode == 0:
        return process.stdout.decode("ascii").strip()
    else:
        pid = os.getpid()

        with open("stdout.%d.log" % pid, "wb") as f:
            f.write(process.stdout)

        with open("stderr.%d.log" % pid, "wb") as f:
            f.write(process.stderr)

        raise RuntimeError(
            (
                "There was an error while running the command %r. "
                "You can find stdout & stderr "
                "in the current working directory. (PID %d)"
            )
            % (args, pid)
        )


class Supervisor:
    def __init__(
        self,
        travis_repo_slug,
        git_remote="origin",
        update_check_interval=TEN_MINUTES,
    ):
        self.update_check_interval = update_check_interval

        # Travis
        self.travis_repo_slug = travis_repo_slug

        # Git
        self.remote = git_remote
        self.branch = _get_output(["git", "symbolic-ref", "--short", "HEAD"])
        self.current_commit_hash = _get_output(["git", "rev-parse", "HEAD"])

        self._force_update_event = threading.Event()

        threading.Thread(target=self._check_for_updates, daemon=True).start()

    def restart(self):
        logger.info("Restarting ... ")

        if hasattr(atexit, "_run_exitfuncs"):
            # We're about to leave in a way that's not expected by
            # Python.
            # This means that some things, including atexit callbacks,
            # won't be run.
            # We want them to run because ircbot.py relies on them, so
            # this is our kind-of CPython hack.
            atexit._run_exitfuncs()

        os.execvp(sys.executable, [sys.executable] + sys.argv)

    def update(self):
        self._force_update_event.set()

    def _upstream_is_newer(self):
        # Pull from upstream and check if there were any new commits.

        command = subprocess.run(
            ["git", "pull", self.remote, self.branch],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if command.returncode == 0:
            new_commit_hash = _get_output(["git", "rev-parse", "HEAD"])

            return new_commit_hash != self.current_commit_hash
        else:
            return False

    def _travis_request(self, endpoint_fmt, *fmt_args, method="get"):
        try:
            token = os.environ["TRAVIS_TOKEN"]
        except KeyError:

            raise LookupError(
                "No TRAVIS_TOKEN environment variable was found."
            ) from None

        fmt_args = tuple(urllib.parse.quote_plus(arg) for arg in fmt_args)

        base_url = "https://api.travis-ci.org"
        url = base_url + (endpoint_fmt % fmt_args)

        headers = {
            "Travis-API-Version": "3",
            "User-Agent": "nettirely supervisor",
            "Authorization": "token " + token,
        }

        response = requests.request(method, url, headers=headers)
        response.raise_for_status()
        return response.json()

    def build_state(self):
        try:
            builds = self._travis_request(
                "/repo/%s/builds?limit=5&branch.name=%s",
                self.travis_repo_slug,
                self.branch,
            )["builds"]
        except LookupError:
            return "unknown"

        return builds[0]["state"]

    def _check_for_updates(self):
        while True:
            if self._upstream_is_newer():
                logger.info("Upstream is newer than local ...")

                build_state = self.build_state()
                if build_state == "passed":
                    logger.info("And the travis build has passed")
                    self.restart()
                else:
                    logger.info("But the build state is %r", build_state)

            # This line sleeps until one of the following two conditions:
            #  1. `self.update_check_interval` seconds pass.
            #  2. Somebody sets _force_update_event.
            self._force_update_event.wait(self.update_check_interval)
            self._force_update_event.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if exc_type is not None:
            logger.exception("Exception was raised in supervised code.")
            traceback.print_exception(exc_type, exc_value, exc_tb)

        self.restart()

        raise RuntimeError("Entered unreachable code.")
