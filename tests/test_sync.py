#!/usr/bin/env python3
"""Tests of the self-update (bin/arch-update-cactus-sync) against a local stand-in for GitHub.

Each test builds a "stable" branch from this working tree in a bare repository, installs it into
an empty HOME the way install.sh does, then pushes new versions and runs the sync: a good one is
taken, a broken one is rolled back, a check cut short is redone, edits in the installed copy are
saved and undone, a development clone is never touched, an unreachable GitHub is reported once.

  python3 tests/test_sync.py
Needs git; the UI self-test inside needs python-pexpect.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=stable"]


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arch-update-sync-test-")
        self.home = os.path.join(self.tmp, "home")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.src = os.path.join(self.tmp, "src")
        self.dest = os.path.join(self.home, ".local/share/arch-update-cactus")
        self.state = os.path.join(self.home, ".local/state/arch-update-auto")
        # no user systemd and no desktop: install.sh skips the timer, nothing reaches the real session
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "TERM": "dumb", "LANG": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1"}
        os.makedirs(self.home)
        # version 1 = this working tree as it is now
        files = git(ROOT, "ls-files", "-co", "--exclude-standard").splitlines()
        for f in files:
            if os.path.isfile(os.path.join(ROOT, f)):
                os.makedirs(os.path.dirname(os.path.join(self.src, f)) or self.src, exist_ok=True)
                shutil.copy2(os.path.join(ROOT, f), os.path.join(self.src, f))
        git(self.src, "init", "-q")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "v1")
        git(self.tmp, "clone", "-q", "--bare", self.src, self.remote)
        git(self.src, "remote", "add", "origin", self.remote)
        # installed the way install.sh does it: a clone of stable, its files linked, marked as checked
        git(self.tmp, "clone", "-q", "--branch", "stable", self.remote, self.dest)
        r = self.run_cmd([os.path.join(self.dest, "install.sh"), "--sync"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "sync.ok"), "w") as f:
            f.write(self.head() + "\n")
        self.v1 = self.head()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, cmd):
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=120)

    def sync(self):
        return self.run_cmd([os.path.join(self.home, ".local/bin/arch-update-cactus-sync")]).returncode

    def head(self):
        return git(self.dest, "rev-parse", "HEAD")

    def push(self, path, text, message, mode="a"):
        with open(os.path.join(self.src, path), mode) as f:
            f.write(text)
        git(self.src, "commit", "-qam", message)
        git(self.src, "push", "-q", "origin", "stable")
        return git(self.src, "rev-parse", "HEAD")

    def read_state(self, name):
        try:
            with open(os.path.join(self.state, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def test_nothing_new(self):
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.head(), self.v1)

    def test_good_version_is_taken(self):
        v2 = self.push("README.md", "\nновая строка\n", "v2")
        self.assertEqual(self.sync(), 10)
        self.assertEqual(self.head(), v2)
        self.assertEqual(self.read_state("sync.ok"), v2)
        self.assertEqual(os.path.realpath(os.path.join(self.home, ".local/bin/arch-update-ui")),
                         os.path.realpath(os.path.join(self.dest, "bin/arch-update-ui")))

    def test_broken_version_is_rolled_back_and_skipped(self):
        bad = self.push("lib/engine.py", "\ndef broken(:\n", "broken")
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(self.read_state("sync.rejected"), bad)
        self.assertEqual(self.sync(), 0)                      # not retried right away
        self.assertEqual(self.head(), self.v1)
        with open(os.path.join(ROOT, "lib/engine.py")) as f:
            fixed = self.push("lib/engine.py", f.read() + "\n# fixed\n", "fixed", mode="w")
        self.assertEqual(self.sync(), 10)
        self.assertEqual(self.head(), fixed)

    def test_check_cut_short_is_redone(self):
        bad = self.push("lib/engine.py", "\ndef broken(:\n", "broken")
        git(self.dest, "fetch", "-q", "origin", "stable")      # as if a sync died right after switching
        git(self.dest, "reset", "-q", "--hard", "FETCH_HEAD")
        self.assertEqual(self.head(), bad)
        self.sync()
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(self.read_state("sync.rejected"), bad)

    def test_edits_in_the_installed_copy_are_saved_and_undone(self):
        with open(os.path.join(self.dest, "README.md"), "a") as f:
            f.write("\nправка руками\n")
        self.assertEqual(self.sync(), 0)
        self.assertEqual(git(self.dest, "status", "--porcelain", "--untracked-files=no"), "")
        patches = [p for p in os.listdir(self.state) if p.startswith("local-changes-")]
        self.assertEqual(len(patches), 1)
        with open(os.path.join(self.state, patches[0])) as f:
            self.assertIn("правка руками", f.read())

    def test_development_clone_is_left_alone(self):
        dev = os.path.join(self.tmp, "dev")
        git(self.tmp, "clone", "-q", self.remote, dev)
        mine = self.push("README.md", "\nчужая версия\n", "remote moves on")
        with open(os.path.join(dev, "README.md"), "a") as f:
            f.write("\nмоя незакоммиченная правка\n")
        before = git(dev, "rev-parse", "HEAD")
        r = self.run_cmd([os.path.join(dev, "bin/arch-update-cactus-sync")])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(git(dev, "rev-parse", "HEAD"), before)
        self.assertNotEqual(before, mine)
        with open(os.path.join(dev, "README.md")) as f:
            self.assertIn("моя незакоммиченная правка", f.read())
        r = self.run_cmd([os.path.join(dev, "install.sh"), "--sync"])
        self.assertNotEqual(r.returncode, 0)                   # never points the machine at a clone
        self.assertEqual(os.path.realpath(os.path.join(self.home, ".local/bin/arch-update-ui")),
                         os.path.realpath(os.path.join(self.dest, "bin/arch-update-ui")))

    def test_unreachable_github_is_logged_once(self):
        git(self.dest, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.sync(), 0)
        with open(os.path.join(self.state, "sync.log")) as f:
            self.assertEqual(f.read().count("не удалось получить обновления"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
