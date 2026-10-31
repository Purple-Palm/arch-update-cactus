#!/usr/bin/env python3
"""Tests of the self-update (bin/arch-update-cactus-sync) against a local stand-in for GitHub.

Each test builds "main" with the tag "stable" (moved by CI once the tests pass) from this working
tree in a bare repository, installs it into an empty HOME the way install.sh does, then pushes new
versions and runs the sync: a good one is taken, a broken one is rolled back, one CI has not
passed is left alone, a check cut short is redone, a launch never opens a version that is still
being checked, edits in the installed copy are saved and undone, a development clone is never
touched, an unreachable GitHub is reported once, and a machine installed from the former "stable"
branch follows the tag.

  python3 tests/test_sync.py
Promote runs CI's promote step, cut out of .github/workflows/test.yml, against the same kind of
stand-in: it creates the tag, only ever moves it forward, and never overwrites a tag it cannot read.
Install does a first install on a clean HOME the way the README does (install.sh piped into bash,
cloning the tag from the stand-in), with stubs for pacman, sudo, arch-update and the rest; it is
skipped as root, which install.sh refuses. Without a terminal its output is plain text that fits
80 columns and the question about update reports is not asked; in a terminal (a pty) the question
comes once, only a yes turns reports on, and --sync, --check and an install whose stdin is not the
keyboard never ask. The tray icon is claimed only when it is there, and a narrow terminal gets
lines that fit it, with commands left whole.
Needs git; the UI self-test inside and the terminal runs need python-pexpect.
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "init.defaultBranch=main"]


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def stand_in_for_github(tmp):
    """A bare repository with this working tree as main and the tag stable on it (CI passed).
    Returns (a checkout to push new versions from, the bare repository)."""
    src, remote = os.path.join(tmp, "src"), os.path.join(tmp, "remote.git")
    for f in git(ROOT, "ls-files", "-co", "--exclude-standard").splitlines():
        if os.path.isfile(os.path.join(ROOT, f)):
            os.makedirs(os.path.dirname(os.path.join(src, f)) or src, exist_ok=True)
            shutil.copy2(os.path.join(ROOT, f), os.path.join(src, f))
    git(src, "init", "-q")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "v1")
    git(tmp, "clone", "-q", "--bare", src, remote)
    git(src, "remote", "add", "origin", remote)
    git(src, "push", "-q", "origin", "HEAD:refs/tags/stable")
    return src, remote


def promote_step():
    """The script of CI's promote step, cut out of the workflow file as GitHub runs it."""
    with open(os.path.join(ROOT, ".github", "workflows", "test.yml")) as f:
        lines = f.read().split("\n")
    start = lines.index("      - name: Move the stable tag to the tested commit")
    assert lines[start + 1] == "        run: |", lines[start + 1]
    body = []
    for line in lines[start + 2:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:])
    return "\n".join(body)


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arch-update-sync-test-")
        self.home = os.path.join(self.tmp, "home")
        self.dest = os.path.join(self.home, ".local/share/arch-update-cactus")
        self.state = os.path.join(self.home, ".local/state/arch-update-auto")
        # no user systemd and no desktop: install.sh skips the timer, nothing reaches the real session
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "TERM": "dumb", "LANG": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1"}
        os.makedirs(self.home)
        # version 1 = this working tree as it is now
        self.src, self.remote = stand_in_for_github(self.tmp)
        # installed the way install.sh does it: a clone of the tag, its files linked, marked as checked
        git(self.tmp, "-c", "advice.detachedHead=false", "clone", "-q", "--branch", "stable",
            self.remote, self.dest)
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

    def push(self, path, text, message, mode="a", ci_passed=True):
        """A new commit on main; ci_passed: CI's promote job then moves the tag to it."""
        with open(os.path.join(self.src, path), mode) as f:
            f.write(text)
        git(self.src, "commit", "-qam", message)
        git(self.src, "push", "-q", "origin", "HEAD:refs/heads/main")
        if ci_passed:
            git(self.src, "push", "-q", "--force", "origin", "HEAD:refs/tags/stable")
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

    def test_a_version_ci_has_not_passed_is_left_alone(self):
        self.push("README.md", "\nновая строка\n", "red or still running", ci_passed=False)
        self.assertEqual(self.sync(), 0)
        self.assertEqual(self.head(), self.v1)

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

    def test_a_launch_never_opens_a_version_still_being_checked(self):
        # The launcher's sync waits 20 s for one already running (the timer's), then lets the
        # window open on a version that passed here, but waits on while one is being checked.
        v2 = self.push("README.md", "\nновая строка\n", "v2")
        fetching = os.path.join(self.tmp, "home2")             # a second machine, its sync still fetching
        shutil.copytree(self.home, fetching, symlinks=True)
        git(self.dest, "fetch", "-q", "origin", "stable")      # this one: switched to v2, check running
        git(self.dest, "reset", "-q", "--hard", "FETCH_HEAD")
        locks, runs = [], {}
        for home in (self.home, fetching):
            f = open(os.path.join(home, ".local/state/arch-update-auto/sync.lock"), "w")
            fcntl.flock(f, fcntl.LOCK_EX)                     # the other sync holds the lock
            locks.append(f)
            runs[home] = subprocess.Popen([os.path.join(home, ".local/share/arch-update-cactus/bin/arch-update-cactus-sync"),
                                           "--quick"], env=dict(self.env, HOME=home))
            self.addCleanup(runs[home].wait)
            self.addCleanup(runs[home].kill)                    # only matters when the test fails
        t0 = time.time()
        self.assertEqual(runs[fetching].wait(timeout=60), 0)
        self.assertGreater(time.time() - t0, 19)
        time.sleep(2)
        self.assertIsNone(runs[self.home].poll())               # v2 has not passed yet: still waiting
        with open(os.path.join(self.state, "sync.ok"), "w") as f:
            f.write(v2 + "\n")                                  # the other sync's check passed
        locks[0].close()
        self.assertEqual(runs[self.home].wait(timeout=60), 0)
        self.assertEqual(self.head(), v2)
        locks[1].close()

    def test_an_install_from_the_stable_branch_follows_the_tag(self):
        # Machines installed before CI moved a tag cloned the branch "stable". Their sync runs the
        # same `git fetch origin stable`, so once the branch makes way for the tag they follow it.
        git(self.remote, "tag", "-d", "stable")
        git(self.remote, "branch", "stable", self.v1)
        shutil.rmtree(self.dest)
        git(self.tmp, "clone", "-q", "--branch", "stable", self.remote, self.dest)
        self.assertEqual(git(self.dest, "branch", "--show-current"), "stable")
        r = self.run_cmd([os.path.join(self.dest, "install.sh"), "--sync"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        v2 = self.push("README.md", "\nновая строка\n", "v2", ci_passed=False)
        git(self.remote, "branch", "-D", "stable")
        git(self.remote, "tag", "stable", v2)
        self.assertEqual(self.sync(), 10)
        self.assertEqual(self.head(), v2)
        self.assertEqual(self.read_state("sync.ok"), v2)

    def test_an_annotated_tag_is_followed_once(self):
        # CI pushes a plain tag; one made by hand with `git tag -a` must not "update" on every run
        v2 = self.push("README.md", "\nновая строка\n", "v2", ci_passed=False)
        git(self.src, "tag", "-a", "-f", "-m", "by hand", "stable", v2)
        git(self.src, "push", "-q", "--force", "origin", "refs/tags/stable")
        self.assertEqual(self.sync(), 10)
        self.assertEqual(self.head(), v2)
        self.assertEqual(self.read_state("sync.ok"), v2)
        self.assertEqual(self.sync(), 0)

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


class Promote(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arch-update-promote-test-")
        self.remote = os.path.join(self.tmp, "remote.git")
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        git(src, "init", "-q")
        self.commits = []
        for i in range(3):
            with open(os.path.join(src, "f"), "w") as f:
                f.write(f"{i}\n")
            git(src, "add", "f")
            git(src, "commit", "-qm", f"c{i}")
            self.commits.append(git(src, "rev-parse", "HEAD"))
        git(src, "checkout", "-q", "--orphan", "other")      # a main force-pushed onto other history
        git(src, "commit", "-qm", "unrelated")
        self.unrelated = git(src, "rev-parse", "HEAD")
        git(self.tmp, "clone", "-q", "--bare", src, self.remote)
        self.script = promote_step()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def promote(self, sha, readable=True):
        """Run the step for commit sha in a fresh checkout, as CI does: (exit status, output)."""
        ci = os.path.join(self.tmp, "ci")
        shutil.rmtree(ci, ignore_errors=True)
        git(self.tmp, "clone", "-q", self.remote, ci)
        if not readable:                  # GitHub does not answer reads; a push would still go through
            git(ci, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))
            git(ci, "remote", "set-url", "--push", "origin", self.remote)
        r = subprocess.run(["bash", "-eo", "pipefail", "-c", self.script], cwd=ci, capture_output=True,
                           text=True, env=dict(os.environ, GITHUB_SHA=sha), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def tag(self):
        return subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/tags/stable"], cwd=self.remote,
                              capture_output=True, text=True).stdout.strip() or None

    def test_the_first_run_creates_a_plain_tag(self):
        self.assertEqual(self.promote(self.commits[0])[0], 0)
        self.assertEqual(self.tag(), self.commits[0])
        self.assertEqual(git(self.remote, "cat-file", "-t", "refs/tags/stable"), "commit")

    def test_the_tag_moves_forward(self):
        self.promote(self.commits[0])
        self.assertEqual(self.promote(self.commits[2])[0], 0)
        self.assertEqual(self.tag(), self.commits[2])

    def test_a_late_rerun_for_an_older_commit_changes_nothing(self):
        self.promote(self.commits[2])
        status, out = self.promote(self.commits[1])
        self.assertEqual(status, 0, out)
        self.assertIn("already contains", out)
        self.assertEqual(self.tag(), self.commits[2])

    def test_a_diverged_main_is_refused(self):
        self.promote(self.commits[2])
        self.assertEqual(self.promote(self.unrelated)[0], 1)
        self.assertEqual(self.tag(), self.commits[2])

    def test_a_tag_it_cannot_read_is_never_overwritten(self):
        self.promote(self.commits[2])
        status, out = self.promote(self.commits[0], readable=False)
        self.assertEqual(status, 1, out)
        self.assertEqual(self.tag(), self.commits[2])


# An Arch machine where everything install.sh would install is already there; nothing is started.
STUBS = {
    "pacman": "exit 0", "sudo": "exit 0", "yay": "exit 0", "kitty": "exit 0", "setsid": "exit 0",
    "fc-list": "echo 'Noto Sans'",
    "systemctl": "exit 1",                  # no user systemd: no timers, nothing reaches a real session
    "pgrep": "exit 1", "pkill": "exit 0",
    # like the real one: its files under XDG_CONFIG_HOME, and it refuses to overwrite either of them
    "arch-update": 'config=${XDG_CONFIG_HOME:-$HOME/.config}\n'
                   'echo "$*" >>"$HOME/arch-update.calls"\n'
                   'case "$*" in\n'
                   '--gen-config)\n'
                   '\t[[ -f $config/arch-update/arch-update.conf ]] && { echo "ERROR: config already exists" >&2; exit 8; }\n'
                   '\tmkdir -p "$config/arch-update"\n'
                   '\tprintf "#KeepOldPackages=3\\n#AURHelper=paru\\n" >"$config/arch-update/arch-update.conf" ;;\n'
                   '"--tray --enable")\n'
                   '\t[[ -f $config/autostart/arch-update-tray.desktop ]] && { echo "ERROR: autostart already exists" >&2; exit 10; }\n'
                   '\tmkdir -p "$config/autostart" && : >"$config/autostart/arch-update-tray.desktop" ;;\n'
                   'esac',
}
QUESTION = "Отправлять отчёты?"                  # the installer's question about update reports
BANNER_ROW = "▄▀█ █▀█ █▀▀ █ █"                    # "ARCH" in the installer's banner
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Install(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh refuses to run as root")
        self.tmp = tempfile.mkdtemp(prefix="arch-update-install-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        _, remote = stand_in_for_github(self.tmp)
        self.tag = git(remote, "rev-parse", "refs/tags/stable")
        stubs = self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        for name, body in STUBS.items():
            self.stub(name, body)
        gitconfig = os.path.join(self.tmp, "gitconfig")          # GitHub is the stand-in
        with open(gitconfig, "w") as f:
            f.write(f'[url "{remote}"]\n\tinsteadOf = https://github.com/Purple-Palm/arch-update-cactus.git\n')
        # only the stand-in: a URL it does not cover fails instead of reaching the real GitHub
        self.env = {"HOME": self.home, "USER": "tester", "PATH": stubs + ":/usr/bin:/bin", "TERM": "dumb",
                    "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": gitconfig,
                    "GIT_ALLOW_PROTOCOL": "file"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, name, body):
        with open(os.path.join(self.stubs, name), "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(os.path.join(self.stubs, name), 0o755)

    def install(self, **env):
        """The README one-liner without a terminal: install.sh piped into bash, in a session of its own,
        so it has no /dev/tty either (as in CI, whoever runs the tests). Returns (exit status, output)."""
        with open(os.path.join(ROOT, "install.sh"), "rb") as f:
            r = subprocess.run(["bash"], stdin=f, env=dict(self.env, **env), capture_output=True, timeout=300,
                               start_new_session=True)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def in_a_terminal(self, *cmd, answer=None, typed_ahead="", size=(24, 80), **env):
        """cmd in a terminal that shows colours (a pty of size rows x columns); typed_ahead is typed
        right away, and the question about update reports, if it comes, gets answer and Enter. Returns
        (exit status, the output as the screen shows it: no colours, each line as its last carriage
        return left it, whether it asked)."""
        import pexpect
        child = pexpect.spawn(cmd[0], list(cmd[1:]), env=dict(self.env, TERM="xterm-256color", **env),
                              encoding="utf-8", dimensions=size, timeout=120)
        child.send(typed_ahead)
        out, asked = "", False
        while child.expect_exact([QUESTION, pexpect.EOF]) == 0:
            out += child.before + QUESTION
            child.expect_exact("[y/N]")
            out += child.before + child.after
            asked = True
            child.sendline(answer or "")
        out += child.before
        child.close()
        screen = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in ANSI.sub("", out).split("\n")]
        return child.exitstatus, "\n".join(screen), asked

    def one_liner_in_a_terminal(self, **kw):
        return self.in_a_terminal("bash", "-c", 'cat "$0" | bash', os.path.join(ROOT, "install.sh"), **kw)

    def reports(self):
        """The user's choice about update reports, as the installer reads it: "включены", "выключены",
        or None while there is none."""
        cli = [sys.executable, os.path.join(self.home, ".local/share/arch-update-cactus/lib/report.py")]
        if subprocess.run(cli + ["--decided"], env=self.env).returncode:
            return None
        return subprocess.run(cli + ["--status"], env=self.env, capture_output=True, text=True).stdout.strip()

    def assertPlainAndNarrow(self, out):
        """Output for a pipe or a log: no colour codes, and no line wider than a Chromebook's 80 columns."""
        self.assertNotIn("\x1b", out)
        self.assertEqual([line for line in out.splitlines() if len(line) > 80], [])

    def test_first_install_on_a_clean_machine(self):
        status, out = self.install(TERM="xterm-256color")       # a desktop terminal's TERM, the output to a pipe
        self.assertEqual(status, 0, out)
        dest = os.path.join(self.home, ".local/share/arch-update-cactus")
        self.assertEqual(git(dest, "rev-parse", "HEAD"), self.tag)
        with open(os.path.join(self.home, ".local/state/arch-update-auto/sync.ok")) as f:
            self.assertEqual(f.read().strip(), self.tag)
        self.assertEqual(os.path.realpath(os.path.join(self.home, ".local/bin/arch-update-launcher")),
                         os.path.realpath(os.path.join(dest, "bin/arch-update-launcher")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".local/share/applications/arch-update.desktop")))
        with open(os.path.join(self.home, ".config/arch-update/arch-update.conf")) as f:
            self.assertEqual(f.read(), "KeepOldPackages=2\nAURHelper=yay\n")
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config/autostart/arch-update-tray.desktop")))
        # no graphical session to show it in: the icon is promised for the next login, not claimed
        self.assertIn("✓ arch-update · настроен, значок появится при входе", out)
        self.assertIn("Готово — Arch Update by Cactus", out)
        # no user systemd here, so no timers: the report must not promise hourly updates
        self.assertNotIn("раз в час", out)
        self.assertIn("фоновые проверки не включились", out)
        # no terminal to answer from: nobody is asked about reports, and the choice stays open
        self.assertNotIn(QUESTION, out)
        self.assertIsNone(self.reports())
        self.assertRegex(out, r"отчёты +выключены")
        # the banner shows once, not again in the copy the bootstrap starts
        self.assertEqual(out.count(BANNER_ROW), 1, out)
        self.assertPlainAndNarrow(out)

    def test_with_user_systemd_the_report_promises_the_timer(self):
        self.stub("systemctl", "exit 0")                    # a desktop session: user systemd, the timers enable
        status, out = self.install()
        self.assertEqual(status, 0, out)
        self.assertIn("новые версии приезжают сами: раз в час, при входе и при запуске", out)    # on one line
        self.assertNotIn("фоновые проверки не включились", out)
        self.assertPlainAndNarrow(out)

    def test_the_tray_icon_is_claimed_only_when_it_is_there(self):
        # arch-update cannot set up the tray's autostart: no icon is promised, the command to try is given
        self.stub("arch-update", '[[ "$*" == "--tray --enable" ]] && { echo "ERROR: no tray" >&2; exit 5; }\n'
                  + STUBS["arch-update"])
        status, out = self.install()
        self.assertEqual(status, 0, out)
        self.assertRegex(out, r"(?m)^  ✓ arch-update · настроен$")
        self.assertIn("  ! автозапуск значка в трее не включился:\n      arch-update --tray --enable\n", out)
        # in a graphical session (a rerun, which sets the autostart up): an applet that never comes up...
        self.stub("arch-update", STUBS["arch-update"])
        status, out = self.install(DISPLAY=":0")
        self.assertEqual(status, 0, out)
        self.assertRegex(out, r"(?m)^  ✓ arch-update · настроен$")
        self.assertIn("  ! значок в трее не запустился:\n      arch-update --tray\n", out)
        # ...and one that runs
        self.stub("pgrep", "exit 0")
        status, out = self.install(DISPLAY=":0")
        self.assertEqual(status, 0, out)
        self.assertIn("  ✓ arch-update · настроен, значок в трее\n", out)
        self.assertNotIn("не запустился", out)
        self.assertNotIn("автозапуск значка", out)

    def test_a_refused_sudo_is_named_and_a_rerun_finishes(self):
        self.stub("pacman", '[[ "$*" == "-Q libnotify" ]] && exit 1\nexit 0')    # one package to install
        self.stub("sudo", 'echo "tester is not in the sudoers file." >&2\nexit 1')
        status, out = self.install()
        self.assertEqual(status, 1, out)
        self.assertIn("sudo -v", out)
        self.assertPlainAndNarrow(out)
        self.stub("sudo", "exit 0")                                            # fixed: the same one-liner
        status, out = self.install()
        self.assertEqual(status, 0, out)
        self.assertIn("Готово", out)

    def test_in_a_terminal_the_one_liner_asks_about_reports_once(self):
        status, out, asked = self.one_liner_in_a_terminal(answer="y")
        self.assertEqual(status, 0, out)
        self.assertTrue(asked, out)
        card = re.sub(r"\s+", " ", out.replace("┃", " "))                     # lines of the card joined
        for said in ("модель устройства и железо", "тип сети без её названия", "журнал обновления",
                     "имя пользователя и компьютера, пароли, адреса, название Wi-Fi",
                     "закрытый репозиторий разработчика на GitHub", "в любой момент: Arch Update → Настройки"):
            self.assertIn(said, card)
        self.assertEqual(self.reports(), "включены")
        self.assertRegex(out, r"отчёты +включены")
        self.assertEqual(out.count(BANNER_ROW), 1, out)
        # the user has chosen: the same one-liner again keeps the choice and does not ask
        status, out, asked = self.one_liner_in_a_terminal()
        self.assertEqual(status, 0, out)
        self.assertFalse(asked, out)
        self.assertEqual(self.reports(), "включены")

    def test_only_a_yes_turns_reports_on(self):
        settings = os.path.join(self.home, ".local/state/arch-update-auto/settings.json")
        # Enter, "н" (where "нет" starts) and anything else is a no
        for answer, chosen in (("", "выключены"), ("н", "выключены"), ("нет", "выключены"), ("n", "выключены"),
                               ("y", "включены"), ("YES", "включены"), ("д", "включены"), ("Да", "включены")):
            with self.subTest(answer=answer):
                if os.path.exists(settings):
                    os.remove(settings)                                         # not chosen yet
                status, out, asked = self.one_liner_in_a_terminal(answer=answer)
                self.assertEqual(status, 0, out)
                self.assertTrue(asked, out)
                self.assertEqual(self.reports(), chosen)

    def test_keys_pressed_during_the_install_are_no_answer(self):
        # an Enter pressed while pacman worked must not answer a question that was not on the screen yet
        status, out, asked = self.one_liner_in_a_terminal(typed_ahead="\r\r", answer="y")
        self.assertEqual(status, 0, out)
        self.assertTrue(asked, out)
        self.assertEqual(self.reports(), "включены")

    def test_sync_and_check_never_ask(self):
        status, out = self.install()                                           # no terminal: still open
        self.assertEqual(status, 0, out)
        installed = os.path.join(self.home, ".local/share/arch-update-cactus/install.sh")
        for mode in ("--sync", "--check"):
            with self.subTest(mode=mode):
                status, out, asked = self.in_a_terminal(installed, mode)
                self.assertEqual(status, 0, out)
                self.assertFalse(asked, out)
        self.assertIsNone(self.reports())

    def test_an_install_without_a_keyboard_does_not_ask(self):
        # the output on a terminal, but stdin not one (a script started it): there is nobody to answer
        status, out = self.install()
        self.assertEqual(status, 0, out)
        installed = os.path.join(self.home, ".local/share/arch-update-cactus/install.sh")
        status, out, asked = self.in_a_terminal("bash", "-c", '"$0" </dev/null', installed)
        self.assertEqual(status, 0, out)
        self.assertIn("Готово", out)
        self.assertFalse(asked, out)
        self.assertIsNone(self.reports())

    def test_the_width_follows_the_terminal(self):
        # 40 columns: every line fits, but for commands, which stay whole so that a copy of one runs
        self.stub("pacman", '[[ "$*" == "-Q gnome-shell-extension-appindicator" ]] && exit 1\nexit 0')
        status, out, asked = self.one_liner_in_a_terminal(size=(24, 40), XDG_CURRENT_DESKTOP="GNOME")
        self.assertEqual(status, 0, out)
        self.assertTrue(asked, out)
        self.assertEqual([line.strip() for line in out.splitlines() if len(line) > 40],
                         ["~/.local/bin/arch-update-launcher --demo",
                          "sudo pacman -S gnome-shell-extension-appindicator"], out)
        # a terminal that does not know its size (0x0: a serial console, ssh -tt) is not a narrow one
        status, out, asked = self.one_liner_in_a_terminal(size=(0, 0))
        self.assertEqual(status, 0, out)
        self.assertEqual(out.count(BANNER_ROW), 1, out)

    def test_arch_update_config_is_found_under_xdg_config_home(self):
        config = os.path.join(self.tmp, "xdg-config")
        status, out = self.install(XDG_CONFIG_HOME=config)
        self.assertEqual(status, 0, out)
        with open(os.path.join(config, "arch-update/arch-update.conf")) as f:
            self.assertEqual(f.read(), "KeepOldPackages=2\nAURHelper=yay\n")
        self.assertTrue(os.path.isfile(os.path.join(config, "autostart/arch-update-tray.desktop")))
        # installing again (a repair) finds both files and asks arch-update for neither
        r = subprocess.run(["bash", os.path.join(self.home, ".local/share/arch-update-cactus/install.sh")],
                           env=dict(self.env, XDG_CONFIG_HOME=config), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("already exists", out)
        with open(os.path.join(self.home, "arch-update.calls")) as f:
            calls = f.read().splitlines()
        self.assertEqual(calls.count("--gen-config"), 1, calls)
        self.assertEqual(calls.count("--tray --enable"), 1, calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
