#!/usr/bin/env python3
"""Tests of the self-update (bin/arch-update-cactus-sync) against a local stand-in for GitHub.

Each test builds "main" with the tag "stable" (moved by CI once the tests pass) from this working
tree in a bare repository, installs it into an empty HOME the way install.sh does, then pushes new
versions and runs the sync: a good one is taken, a broken one is rolled back, one CI has not
passed is left alone, a check cut short is redone, edits in the installed copy are saved and
undone, a development clone is never touched, an unreachable GitHub is reported once, and a
machine installed from the former "stable" branch follows the tag.

  python3 tests/test_sync.py
Promote runs CI's promote step, cut out of .github/workflows/test.yml, against the same kind of
stand-in: it creates the tag, only ever moves it forward, and never overwrites a tag it cannot read.
Install does a first install on a clean HOME the way the README does (install.sh piped into bash,
cloning the tag from the stand-in), with stubs for pacman, sudo, arch-update and the rest; it is
skipped as root, which install.sh refuses.
Needs git; the UI self-test inside needs python-pexpect.
"""
import os
import shutil
import subprocess
import tempfile
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
    "pgrep": "exit 1",
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


class Install(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh refuses to run as root")
        self.tmp = tempfile.mkdtemp(prefix="arch-update-install-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        _, remote = stand_in_for_github(self.tmp)
        self.tag = git(remote, "rev-parse", "refs/tags/stable")
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        for name, body in STUBS.items():
            with open(os.path.join(stubs, name), "w") as f:
                f.write("#!/bin/bash\n" + body + "\n")
            os.chmod(os.path.join(stubs, name), 0o755)
        gitconfig = os.path.join(self.tmp, "gitconfig")          # GitHub is the stand-in
        with open(gitconfig, "w") as f:
            f.write(f'[url "{remote}"]\n\tinsteadOf = https://github.com/Purple-Palm/arch-update-cactus.git\n')
        # only the stand-in: a URL it does not cover fails instead of reaching the real GitHub
        self.env = {"HOME": self.home, "USER": "tester", "PATH": stubs + ":/usr/bin:/bin", "TERM": "dumb",
                    "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": gitconfig,
                    "GIT_ALLOW_PROTOCOL": "file"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, **env):
        """The README one-liner: install.sh piped into bash. Returns (exit status, output)."""
        with open(os.path.join(ROOT, "install.sh"), "rb") as f:
            r = subprocess.run(["bash"], stdin=f, env=dict(self.env, **env), capture_output=True, timeout=300)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")

    def test_first_install_on_a_clean_machine(self):
        status, out = self.install()
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
        self.assertIn("готово", out)

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
