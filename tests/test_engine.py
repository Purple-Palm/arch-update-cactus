#!/usr/bin/env python3
"""Unit tests for the parts of the engine that differ between machines: how prompts are
answered (sudo asks for a password on the laptops) and what the post-update checks conclude
on kernels, boot files and DKMS setups other than the main PC's.

  python3 tests/test_engine.py
"""
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
os.environ["HOME"] = tempfile.mkdtemp(prefix="arch-update-test-home-")   # logs go here, before the import
os.environ["ARCH_UPDATE_AUTO_NOTIFY"] = "0"
sys.path.insert(0, os.path.join(ROOT, "lib"))
import engine  # noqa: E402


def decide(*lines):
    """What the responder does when the program waits on the last of these lines."""
    r = engine.Responder(child=None, stop_on_news=True, manual=False)
    r.tail = "\n".join(lines)
    d = r.decide()
    return d and (d[0], d[1])


class Prompts(unittest.TestCase):
    def test_sudo_password_goes_to_the_user(self):
        self.assertEqual(decide("[sudo] password for user: "), ("запрос пароля", engine.HANDOVER))
        self.assertEqual(decide("Password: "), ("запрос пароля", engine.HANDOVER))
        self.assertEqual(decide("doas (user@cb) password: "), ("запрос пароля", engine.HANDOVER))

    def test_routine_questions_are_answered(self):
        self.assertEqual(decide("==> Proceed with update? [Y/n] "), ("proceed with update", engine.YES))
        self.assertEqual(decide(":: Proceed with installation? [Y/n] "), ("pacman proceed", engine.YES))
        self.assertEqual(decide(':: Import PGP key 1234, "Someone"? [Y/n] '), ("import PGP key", engine.YES))

    def test_risky_questions_are_not(self):
        self.assertEqual(decide(":: a and b are in conflict. Remove b? [y/N] "),
                         ("конфликт пакетов", engine.HANDOVER))
        self.assertEqual(decide(":: Do you want to remove these packages? [Y/n] "),
                         ("pacman хочет удалить пакеты", engine.HANDOVER))

    def test_unknown_question_is_not_guessed(self):
        self.assertIsNone(decide("Format the disk? [Y/n] "))

    def test_paru_and_flatpak_end_with_a_colon(self):
        self.assertEqual(decide(":: Proceed to review? [Y/n]: "), ("paru review", engine.YES))
        self.assertEqual(decide(":: Proceed with installation? [Y/n]: "), ("pacman proceed", engine.YES))
        self.assertEqual(decide(":: Remove make dependencies after install? [y/N]: "),
                         ("yay remove make deps", engine.YES))
        self.assertEqual(decide("Proceed with these changes to the system installation? [Y/n]: "),
                         ("flatpak proceed", engine.YES))

    def test_unknown_prompts_are_recognised_as_questions(self):
        for line in ("Which do you want to use (0 to abort)? [0-2]: ", "Delete everything? [y/N]: ",
                     "Continue? ", "Enter a selection: "):
            self.assertTrue(engine.UNKNOWN_PROMPT.search(line.strip()), line)
        self.assertFalse(engine.UNKNOWN_PROMPT.search("[1/100] Compiling foo.cpp"))

    def test_wrong_password_is_explained(self):
        label, action = decide("[sudo] password for user: ", "Sorry, try again.", "[sudo] password for user: ")
        self.assertEqual(action, engine.HANDOVER)
        self.assertIn("не подошёл", label)

    def test_stale_keyring_is_explained(self):
        label, _ = decide("error: linux: signature from \"Someone\" is unknown trust",
                          ":: File /var/cache/pacman/pkg/linux.pkg.tar.zst is corrupted "
                          "(invalid or corrupted package (PGP signature)).",
                          "Do you want to delete it? [Y/n] ")
        self.assertIn("keyring", label)

    def test_any_password_question_is_masked(self):
        r = engine.Responder(child=None, stop_on_news=True, manual=False)
        saved = engine.LOG, engine.VIEW
        self.addCleanup(lambda: setattr(engine, "LOG", saved[0]) or setattr(engine, "VIEW", saved[1]))
        engine.LOG = types.SimpleNamespace(event=lambda *a: None)
        engine.VIEW = types.SimpleNamespace(handover=lambda *a: None)
        for line, secret in (("Password for alice@chromebook: ", True), ("Пароль: ", True),
                             (":: a and b are in conflict. Remove b? [y/N] ", False)):
            r.tail = line
            r.hand_over("незнакомый вопрос")
            self.assertEqual(r.secret, secret, line)


class Machine(unittest.TestCase):
    """A fake /usr/lib/modules, /etc/mkinitcpio.d, /usr/src and power supplies per test; pacman
    "owns" the module directories registered with kernel()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = engine.MODULES_DIR, engine.PRESET_DIR, engine.DKMS_SRC, engine.POWER_DIR, engine.run
        engine.MODULES_DIR = os.path.join(self.tmp, "modules")
        engine.PRESET_DIR = os.path.join(self.tmp, "presets")
        engine.DKMS_SRC = os.path.join(self.tmp, "src")
        engine.POWER_DIR = os.path.join(self.tmp, "power")
        os.makedirs(engine.PRESET_DIR)
        self.dkms = ""
        self.owners = {}
        real_run = engine.run

        def fake_run(cmd, *a, **k):
            if cmd[0] == "dkms":
                return self.dkms
            if cmd[:2] == ["pacman", "-Qo"]:
                return "".join(f"{f} is owned by {self.owners[f]} 1.0-1\n" for f in cmd[2:] if f in self.owners)
            if cmd[0] in ("sudo", "systemctl", "pacdiff", "pgrep", "findmnt"):
                return ""
            return real_run(cmd, *a, **k)
        engine.run = fake_run
        engine.LOG = engine.RunLog()

    def tearDown(self):
        engine.MODULES_DIR, engine.PRESET_DIR, engine.DKMS_SRC, engine.POWER_DIR, engine.run = self.saved
        engine.LOG.txt.close()
        engine.LOG.raw.close()

    def kernel(self, pkg, kver, preset=None, owned=True):
        d = os.path.join(engine.MODULES_DIR, kver)
        os.makedirs(d)
        open(os.path.join(d, "modules.builtin"), "w").close()
        if owned:
            self.owners[os.path.join(d, "modules.builtin")] = pkg
        if preset is not None:
            with open(os.path.join(engine.PRESET_DIR, pkg + ".preset"), "w") as f:
                f.write(preset)

    def supply(self, name, **files):
        d = os.path.join(engine.POWER_DIR, name)
        os.makedirs(d)
        for k, v in files.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(f"{v}\n")

    def boot_file(self, name, data=b"x" * 64, mode=0o644):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path

    def checks(self):
        return engine.system_checks({}, 0, types.SimpleNamespace(failures=set(), notes=set()), 0)

    def test_dkms_status_formats(self):
        self.dkms = ("nvidia/615.78, 7.2.11-arch1-1, x86_64: installed\n"
                     "v4l2loopback, 0.13, 7.2.11-arch1-1, x86_64: installed\n"
                     "zfs/2.3: added\n")
        self.assertEqual(engine.dkms_built(), {"nvidia": {"7.2.11-arch1-1"}, "v4l2loopback": {"7.2.11-arch1-1"},
                                               "zfs": set()})

    def test_preset_files(self):
        self.kernel("linux-lts", "6.12.50-1-lts", 'ALL_kver="/boot/vmlinuz-linux-lts"\n'
                    "PRESETS=('default' 'fallback')\n"
                    'default_image="/boot/initramfs-linux-lts.img"\n'
                    'fallback_image="/boot/initramfs-linux-lts-fallback.img"\n')
        self.assertEqual(engine.preset_files("linux-lts"), [
            ("kernel", "/boot/vmlinuz-linux-lts"), ("image", "/boot/initramfs-linux-lts.img"),
            ("image", "/boot/initramfs-linux-lts-fallback.img")])

    def test_other_boot_setup_is_not_a_problem(self):
        self.kernel("linux-aarch64", "6.17.1-1-aarch64")            # no preset: booster/depthcharge/…
        r = self.checks()
        self.assertEqual(r["problems"], [])

    def test_root_only_initramfs_is_not_a_problem(self):
        kernel = self.boot_file("vmlinuz-linux")
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.kernel("linux", "7.2.11-arch1-1", f'ALL_kver="{kernel}"\nPRESETS=(default)\ndefault_image="{image}"\n')
        if os.access(image, os.R_OK):
            self.skipTest("running as root: every file is readable")
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs на месте", r["ok"])

    def test_missing_kernel_file_is_a_problem(self):
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.kernel("linux", "7.2.11-arch1-1", f'ALL_kver="{self.tmp}/gone"\nPRESETS=(default)\ndefault_image="{image}"\n')
        r = self.checks()
        self.assertTrue(any("НЕ перезагружайся" in p and "/gone" in p for p in r["problems"]), r["problems"])

    def test_dkms_module_missing_for_a_kernel(self):
        self.kernel("linux", "7.2.12-arch1-1")
        self.dkms = "nvidia/615.78, 7.2.11-arch1-1, x86_64: installed\n"
        r = self.checks()
        self.assertTrue(any("NVIDIA не собран под ядро 7.2.12-arch1-1" in p for p in r["problems"]), r["problems"])

    def test_failed_nvidia_build_on_the_only_kernel(self):
        # the dkms hook never fails pacman; the old kernel's build is gone, the new one didn't build
        self.kernel("linux", "7.2.12-arch1-1")
        self.dkms = "nvidia/615.78.08: added\n"
        r = self.checks()
        self.assertTrue(any("NVIDIA не собран под ядро 7.2.12-arch1-1" in p for p in r["problems"]), r["problems"])

    def test_module_limited_to_other_kernels_is_not_missing(self):
        self.kernel("linux", "7.2.12-arch1-1")
        self.kernel("linux-lts", "6.12.50-1-lts")
        self.dkms = "lts-only/1.0, 6.12.50-1-lts, x86_64: installed\n"
        os.makedirs(os.path.join(engine.DKMS_SRC, "lts-only-1.0"))
        with open(os.path.join(engine.DKMS_SRC, "lts-only-1.0", "dkms.conf"), "w") as f:
            f.write('PACKAGE_NAME="lts-only"\nBUILD_EXCLUSIVE_KERNEL="^6\\.12"\n')
        r = self.checks()
        self.assertFalse(any("lts-only" in p for p in r["problems"]), r["problems"])

    def test_kernels_are_found_by_owner_not_by_leftover_directories(self):
        self.kernel("linux", "7.2.12-arch1-1")
        self.kernel("linux", "7.2.11-arch1-1", owned=False)     # kernel-modules-hook's copy of the old one
        self.assertEqual(engine.kernel_packages(), {"linux": "7.2.12-arch1-1"})

    def test_running_kernel_replaced_asks_for_a_reboot(self):
        self.kernel("linux", "0.0.1-not-the-running-one")
        r = self.checks()
        self.assertTrue(any("ядро обновилось" in w for w in r["warnings"]), r["warnings"])

    def test_without_known_kernels_arch_updates_reboot_note_counts(self):
        r = engine.system_checks({}, 0, types.SimpleNamespace(failures=set(), notes={"reboot"}), 0)
        self.assertTrue(any("перезагруз" in w for w in r["warnings"]), r["warnings"])

    def test_only_the_machines_own_battery_counts(self):
        self.supply("hidpp_battery_0", type="Battery", scope="Device", capacity=18, status="Discharging")
        self.supply("AC", type="Mains", online=1)
        self.assertIsNone(engine.battery())
        self.supply("BAT0", type="Battery", capacity=64, status="Charging")
        self.assertEqual(engine.battery(), (64, False))

    def test_no_dkms_no_nvidia_lines(self):
        self.kernel("linux", "7.2.11-arch1-1")
        r = self.checks()
        self.assertFalse(any("NVIDIA" in line or "DKMS" in line for line in r["problems"] + r["ok"]))


class SudoOnce(unittest.TestCase):
    """A machine whose sudo asks for a password (the laptops): asked once, at the start, through
    the handover; the run then continues with a fresh ticket and its exit status comes through."""

    def test_password_once_then_the_update_runs(self):
        tmp = self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "bin"))
        scripts = {
            # fake sudo: "-n …" succeeds only with a ticket, "-v" asks for the password
            "sudo": '#!/bin/bash\ncase "$1" in\n-n) [ -f "$T/ticket" ] ;;\n'
                    '-v) read -rsp "[sudo] password for tester: " pw; echo\n'
                    '    [ "$pw" = secret ] && touch "$T/ticket" || { echo "Sorry, try again."; exit 1; } ;;\nesac\n',
            "fake-update": '#!/bin/bash\n[ -f "$T/ticket" ] && echo "ticket ok"\n'
                           'read -rp "==> Proceed with update? [Y/n] " a; echo "answer=$a"; exit 3\n',
        }
        for name, text in scripts.items():
            path = os.path.join(tmp, "bin", name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)
        env = {"PATH": os.path.join(tmp, "bin") + ":" + os.environ["PATH"], "T": tmp}
        saved_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        saved_stdin = os.dup(0)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)                      # no keyboard: the handover below types for the user

        def restore():
            os.dup2(saved_stdin, 0)
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.assertTrue(engine.sudo_asks_password())

        class View(engine.TerminalView):
            asked = []

            def out(self, data):
                pass

            def handover(self, why, line):
                self.asked.append((why, line, engine.RESPONDER.secret))
                engine.RESPONDER.user_input(b"secret\r")
        real_unmounted = engine.unmounted_boot
        self.addCleanup(lambda: setattr(engine, "unmounted_boot", real_unmounted))
        engine.unmounted_boot = lambda: []    # whatever this test machine's fstab says
        view = View()
        status, _, _ = engine.run_update(view, cmd="fake-update", summary=False)
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        with open(engine.LOG.path) as f:
            log = f.read()
        self.assertEqual(status, 3)
        self.assertEqual(len(View.asked), 1, View.asked)
        self.assertTrue(View.asked[0][2], "the password must be masked")
        self.assertIn("ticket ok", log)
        self.assertIn("answer=y", log)
        self.assertNotIn("secret", log)

        # a refused password: nothing has started, and the summary says exactly that
        View.asked.clear()
        os.remove(os.path.join(self.tmp, "ticket"))
        View.handover = lambda self, why, line: (View.asked.append(why), engine.RESPONDER.user_input(b"wrong\r"))
        status, res, _ = engine.run_update(View(), cmd="fake-update", summary=True)
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        self.assertEqual(status, 125)
        self.assertFalse(res["started"])
        self.assertEqual(res["problems"], [])
        self.assertIn("не начиналось", res["warnings"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
