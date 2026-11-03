#!/usr/bin/env python3
"""Unit tests for the parts of the engine that differ between machines: how prompts are
answered (sudo asks for a password on the laptops, services are restarted or not), what the
post-update checks conclude on kernels, boot files and DKMS setups other than the main PC's,
what makes a verdict yellow and what only a grey note, the summary each update leaves next to
its journal (the menu's warning until a reboot or a fix), the fixes offered for each problem
with booting, and the look into a closed /boot that the update's own sudo session takes.

  python3 tests/test_engine.py
"""
import glob
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
os.environ["HOME"] = tempfile.mkdtemp(prefix="arch-update-test-home-")   # logs go here, before the import
os.environ["ARCH_UPDATE_AUTO_NOTIFY"] = "0"
os.environ.pop("ARCH_UPDATE_AUTO_LOGDIR", None)
os.environ["ARCH_UPDATE_REPORT_URL"] = ""        # nothing here may ever reach the real relay
sys.path.insert(0, os.path.join(ROOT, "lib"))
import engine  # noqa: E402
import fixes  # noqa: E402

KVER = "7.2.11-arch1-1"


def decide(*lines):
    """What the responder does when the program waits on the last of these lines."""
    r = engine.Responder(child=None, stop_on_news=True, manual=False)
    r.tail = "\n".join(lines)
    d = r.decide()
    return d and (d[0], d[1])


def services_tail(*units, head=True):
    """What arch-update's restart_services.sh prints, the question last (colour-stripped)."""
    text = "==> Services:\nThe following services require a post upgrade restart\n\n" if head else ""
    text += "".join(f"{i} - {u}\n" for i, u in enumerate(units, 1))
    return text + ('\n-> Select the service(s) to restart (e.g. 1 3 5), select 0 to restart them all or press '
                   '"enter" to continue without restarting the service(s): ')


def write(path, text, mode=0o755):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class Patching(unittest.TestCase):
    def patch(self, obj, name, value):
        self.addCleanup(setattr, obj, name, getattr(obj, name))
        setattr(obj, name, value)

    def env(self, **values):
        for k, v in values.items():
            self.addCleanup(lambda k=k, old=os.environ.get(k): os.environ.pop(k, None) if old is None
                            else os.environ.__setitem__(k, old))
            os.environ[k] = v


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


class Services(Patching):
    """arch-update lists the services that still run replaced files (checkservices) and asks which to restart.
    The setting off: Enter, as always. On: the numbers of those safe to restart while the desktop runs."""

    def responder(self, *units, restart=True, head=True, dm="sddm.service", own=""):
        r = engine.Responder(child=None, stop_on_news=True, manual=False, restart_services=restart)
        r._dm, r._own = dm, own
        r.tail = services_tail(*units, head=head)
        return r

    def test_the_list_is_read_from_what_arch_update_printed(self):
        found, whole = engine.parse_services(services_tail("cups.service", "sshd.service"))
        self.assertEqual(found, [(1, "cups.service"), (2, "sshd.service")])
        self.assertTrue(whole)
        found, whole = engine.parse_services(services_tail("cups.service", head=False))   # the head cut off
        self.assertEqual(found, [(1, "cups.service")])
        self.assertFalse(whole)

    def test_setting_off_restarts_nothing(self):
        r = self.responder("cups.service", "sshd.service", restart=False)
        self.assertEqual(r.decide(), ("restart services", engine.ENTER, "services"))
        self.assertEqual(r.services["listed"], ["cups.service", "sshd.service"])
        self.assertEqual(r.services["asked"], [])

    def test_setting_on_restarts_only_the_safe_ones(self):
        units = ["cups.service", "dbus-broker.service", "sshd.service", "NetworkManager.service", "sddm.service",
                 "getty@tty1.service", "user@1000.service", "polkit.service", "kiosk.service",
                 "systemd-logind.service", "nvidia-persistenced.service", "bluetooth.service", "dbus.service",
                 "user-runtime-dir@1000.service", "iwd.service", "systemd-journald.service"]
        r = self.responder(*units, dm="sddm.service", own="kiosk.service")
        label, answer, note = r.decide()
        self.assertEqual((label, answer, note), ("restart safe services", "1 3 16\r", "services"))
        self.assertEqual(r.services["asked"], ["cups.service", "sshd.service", "systemd-journald.service"])

    def test_all_safe_is_zero(self):
        self.assertEqual(self.responder("cups.service", "sshd.service").decide()[1], "0\r")

    def test_a_list_not_seen_whole_restarts_nothing(self):
        # A number is a line of arch-update's list: without its header, a line could be from something else.
        r = self.responder("cups.service", "sshd.service", head=False)
        self.assertEqual(r.decide(), ("restart services", engine.ENTER, "services"))
        r = self.responder("cups.service", "sshd.service")
        r.tail = r.tail.replace("2 - sshd", "3 - sshd")                     # numbers that do not follow on
        self.assertEqual(r.decide()[1], engine.ENTER)

    def test_none_safe_is_enter(self):
        r = self.responder("dbus-broker.service", "systemd-logind.service")
        self.assertEqual(r.decide(), ("restart services", engine.ENTER, "services"))

    def test_an_unattended_manual_run_restarts_nothing(self):
        r = self.responder("cups.service")
        r.safe_only = True
        self.assertEqual(r.decide()[1], engine.ENTER)

    def test_the_display_manager_and_our_own_service_are_found(self):
        cgroup = os.path.join(tempfile.mkdtemp(), "cgroup")
        write(cgroup, "0::/system.slice/cage@tty1.service\n", 0o644)
        self.patch(engine, "CGROUP", cgroup)
        self.assertEqual(engine.own_service(), "cage@tty1.service")
        write(cgroup, "0::/user.slice/user-1000.slice/session-2.scope\n", 0o644)
        self.assertEqual(engine.own_service(), "")
        self.patch(engine, "run", lambda cmd, **k: "lemurs-ng.service\n" if cmd[:2] == ["systemctl", "show"] else "")
        self.assertEqual(engine.display_manager(), "lemurs-ng.service")
        self.assertTrue(engine.keeps_running("lemurs-ng.service"))
        self.assertFalse(engine.keeps_running("cups.service"))
        self.assertTrue(engine.keeps_running("odd name; rm -rf"))       # not a plain unit name: never touched

    def test_what_arch_update_said_about_the_restarts_is_recorded(self):
        r = self.responder("cups.service", "sshd.service", "dbus-broker.service", restart=False)
        r.watch_services(r.tail)
        r.watch_services("==> The cups.service service has been successfully restarted\n"
                         "==> ERROR: An error has occurred during the restart of the sshd.service service\n")
        rec = engine.services_record(r)
        self.assertEqual(rec["restarted"], ["cups.service"])
        self.assertEqual(rec["failed"], ["sshd.service"])
        self.assertEqual(rec["skipped"], ["dbus-broker.service"])
        self.assertEqual(rec["later"], ["dbus-broker.service"])
        self.assertEqual(rec["waiting"], [])

    def test_all_at_once_that_failed_is_told_by_the_failed_state(self):
        r = self.responder("cups.service", "sshd.service")
        r.decide()
        r.watch_services("==> ERROR: An error has occurred during the service(s) restart\n")
        rec = engine.services_record(r, failed_now=["sshd.service"])
        self.assertEqual((rec["restarted"], rec["failed"]), (["cups.service"], ["sshd.service"]))

    def test_the_way_to_the_network_and_package_daemons_keep_running(self):
        for unit in ("wpa_supplicant-nl80211@wlan0.service", "wpa_supplicant-wired@eth0.service",
                     "netctl-ifplugd@eth0.service", "systemd-resolved.service", "mullvad-daemon.service",
                     "nordvpnd.service", "warp-svc.service", "netbird.service", "virtlogd.service",
                     "virtlockd.service", "packagekit.service", "pamac-daemon.service"):
            self.assertTrue(engine.keeps_running(unit, "", ""), unit)
        self.assertFalse(engine.keeps_running("systemd-journald.service", "", ""))

    def test_the_restart_command_never_takes_an_unsafe_service(self):
        self.patch(engine, "display_manager", lambda: "sddm.service")
        self.patch(engine, "own_service", lambda: "")
        self.assertEqual(engine.services_command(["cups.service", "dbus.service", "sddm.service", "cups.service"]),
                         ["sudo", "systemctl", "restart", "--", "cups.service"])
        self.assertEqual(engine.services_command(["polkit.service"]), [])


class Machine(Patching):
    """A fake /usr/lib/modules, /etc/mkinitcpio.d, /usr/src and power supplies per test; pacman
    "owns" the module directories registered with kernel(). Each test has its own journal folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = engine.MODULES_DIR, engine.PRESET_DIR, engine.DKMS_SRC, engine.POWER_DIR, engine.run
        engine.MODULES_DIR = os.path.join(self.tmp, "modules")
        engine.PRESET_DIR = os.path.join(self.tmp, "presets")
        engine.DKMS_SRC = os.path.join(self.tmp, "src")
        engine.POWER_DIR = os.path.join(self.tmp, "power")
        os.makedirs(engine.PRESET_DIR)
        self.env(ARCH_UPDATE_AUTO_LOGDIR=os.path.join(self.tmp, "logs"))
        self.dkms = ""
        self.sbctl = ""
        self.probe = ""                 # what the probe prints now ("": sudo would ask, nothing was looked at)
        self.probed = []
        self.orphans = []
        self.pacdiff = []
        self.failed = []
        self.dm = "display-manager.service"
        self.owners = {}
        self.after = {}                 # pacman -Q after the update
        self.newer = {}                 # pacman -Qu: what the repository has newer
        self.runs = 0
        real_run = engine.run

        def fake_run(cmd, *a, **k):
            if cmd[0] == "dkms":
                return self.dkms
            if cmd[:2] == ["pacman", "-Qo"]:
                return "".join(f"{f} is owned by {self.owners[f]} 1.0-1\n" for f in cmd[2:] if f in self.owners)
            if cmd[:2] == ["pacman", "-Qtdq"]:
                return "".join(f"{p}\n" for p in self.orphans)
            if cmd[:2] == ["pacman", "-Qu"]:
                return "".join(f"{p} {self.after.get(p, '1')} -> {self.newer[p]}\n" for p in cmd[2:] if p in self.newer)
            if cmd[:4] == ["sudo", "-n", "bash", "-c"] and cmd[5:6] == ["probe"]:
                self.probed.append(cmd[6:])
                return self.probe
            if cmd == ["sudo", "-n", "sbctl", "verify"]:
                return self.sbctl
            if cmd[:2] == ["systemctl", "show"]:
                return self.dm + "\n"
            if cmd[:2] == ["systemctl", "--failed"]:
                return "".join(f"{u} loaded failed failed Something\n" for u in self.failed)
            if cmd[0] == "pacdiff":
                return "".join(f"{p}\n" for p in self.pacdiff)
            if cmd[0] in ("sudo", "systemctl", "pgrep", "findmnt"):
                return ""
            return real_run(cmd, *a, **k)
        engine.run = fake_run
        self.patch(engine, "installed_versions", lambda: dict(self.after))
        self.patch(engine, "running_kernel", lambda: KVER)
        self.patch(engine, "is_chromebook", lambda: False)
        self.patch(engine, "PACMAN_CACHE", os.path.join(self.tmp, "pkg"))
        # lsinitcpio for the images the user may read: the kernel version written next to the image; as mkinitcpio
        # builds them, its folder always, its module list only when there are modules in it (not with .nomods)
        stub = os.path.join(self.tmp, "bin")
        os.makedirs(stub)
        write(os.path.join(stub, "lsinitcpio"), '#!/bin/bash\n[ -s "$1" ] || exit 1\nk=$(cat "$1.kver") || exit 1\n'
                                                'echo "usr/lib/modules/$k/kernel"\n'
                                                '[ -e "$1.nomods" ] || echo "usr/lib/modules/$k/modules.dep.bin"\n')
        self.env(PATH=stub + os.pathsep + os.environ["PATH"])
        self.pacman_log("")
        engine.LOG = engine.RunLog()

    def tearDown(self):
        engine.MODULES_DIR, engine.PRESET_DIR, engine.DKMS_SRC, engine.POWER_DIR, engine.run = self.saved
        engine.LOG.txt.close()
        engine.LOG.raw.close()

    def new_run(self):
        """The journal of a later update (journals are named after the second they start in)."""
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        engine.LOG = engine.RunLog()
        path = os.path.join(engine.LOG.dir, f"2099-01-01_00-00-{self.runs:02d}.log")
        self.runs += 1
        os.replace(engine.LOG.path, path)
        os.replace(engine.LOG.path[:-4] + ".raw", path[:-4] + ".raw")
        engine.LOG.path = path

    def kernel(self, pkg, kver, preset=None, owned=True, index=True):
        """A kernel as its package and pacman's depmod hook leave it: its modules listed (modules.order) and, unless
        depmod failed (index=False), their module list (modules.dep.bin)."""
        d = os.path.join(engine.MODULES_DIR, kver)
        os.makedirs(d)
        open(os.path.join(d, "modules.builtin"), "w").close()
        write(os.path.join(d, "modules.order"), "kernel/drivers/nvme/host/nvme.ko\n", 0o644)
        if index:
            write(os.path.join(d, "modules.dep.bin"), "index", 0o644)
        if owned:
            self.owners[os.path.join(d, "modules.builtin")] = pkg
        if preset is not None:
            with open(os.path.join(engine.PRESET_DIR, pkg + ".preset"), "w") as f:
                f.write(preset)

    def linux(self, kernel, image, **kw):
        self.kernel("linux", KVER, f'ALL_kver="{kernel}"\nPRESETS=(default)\ndefault_image="{image}"\n', **kw)

    def supply(self, name, **files):
        d = os.path.join(engine.POWER_DIR, name)
        os.makedirs(d)
        for k, v in files.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(f"{v}\n")

    def boot_file(self, name, data=b"x" * 64, mode=0o644, kver=KVER, modules=True):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ".kver", "w") as f:
            f.write(kver)
        if not modules:                 # an image mkinitcpio put no module into
            open(path + ".nomods", "w").close()
        os.chmod(path, mode)
        return path

    def closed_boot(self, *names):
        """Boot files in a folder the user may not look into: archinstall mounts the ESP at /boot
        with fmask=0077,dmask=0077, so a stat by the user fails with EACCES, not ENOENT."""
        d = os.path.join(self.tmp, "boot")
        os.makedirs(d)
        paths = [self.boot_file(os.path.join("boot", n)) for n in names]
        os.chmod(d, 0o600)
        self.addCleanup(os.chmod, d, 0o700)
        if os.access(paths[0], os.F_OK):
            self.skipTest("running as root: every folder is open")
        return paths

    @staticmethod
    def probe_says(sizes=(), images=(), sbctl=None, without_modules=()):
        """What a probe as root prints: sizes [(path, bytes or None: no such file)], images [(path, status,
        [kernel versions])] (with their module lists, but for the images in without_modules), sbctl's output."""
        lines = ["sudo-ok"]
        for path, size in sizes:
            lines += [f"file {path}"] + ([f"size {size} {path}"] if size is not None else [])
        for path, rc, kvers in images:
            lines += [f"image {rc} {','.join(kvers) or '-'} {path}",
                      f"indexed {'-' if path in without_modules else ','.join(kvers) or '-'} {path}"]
        if sbctl is not None:
            lines += ["sbctl"] + [f"sbctl {line}" for line in sbctl.splitlines()]
        return "\n".join(lines) + "\n"

    def pacman_log(self, text):
        path = os.path.join(self.tmp, "pacman.log")
        with open(path, "w") as f:
            f.write(text)
        self.patch(engine, "PACMAN_LOG", path)

    def checks(self, before=None, notes=(), failures=(), carry=False, **kw):
        responder = types.SimpleNamespace(failures=set(failures), notes=set(notes))
        return engine.system_checks(dict(before or {}), kw.pop("exitstatus", 0), responder, 0, carry=carry, **kw)

    def summary(self, res):
        with open(engine.summary_path(res["log"]), encoding="utf-8") as f:
            return json.load(f)

    def fix_keys(self, res):
        return [f["key"] for f in res["fixes"]]

    def secure_boot_machine(self):
        """Secure Boot on, sbctl installed, one kernel file for sbctl to verify."""
        kernel = self.boot_file("vmlinuz-linux")
        self.kernel("linux", KVER, f'ALL_kver="{kernel}"\nPRESETS=(default)\n')
        self.patch(engine, "secure_boot_on", lambda: True)
        self.patch(engine.shutil, "which", lambda name: f"/usr/bin/{name}")
        return kernel

    # ── Secure Boot

    def test_signed_boot_files_pass(self):
        self.sbctl = f"✓ {self.secure_boot_machine()} is signed\n"
        self.assertIn("подпись Secure Boot в порядке", self.checks()["ok"])

    def test_an_unsigned_kernel_is_a_problem_with_a_fix(self):
        kernel = self.secure_boot_machine()
        self.sbctl = f"✗ {kernel} is not signed\n"
        r = self.checks()
        self.assertTrue(any("не подписан" in p for p in r["problems"]), r["problems"])
        self.assertEqual(r["fixes"][-1]["argv"], ["sudo", "sbctl", "sign", "-s", kernel])

    def test_signatures_that_could_not_be_checked_are_said_out_loud(self):
        # After the run on a machine whose sudo asks for a password, `sudo -n` finds no ticket (it went
        # to the update's own terminal), so sbctl prints nothing: that must not pass as "all clear".
        self.secure_boot_machine()
        r = self.checks()
        self.assertTrue(any("sbctl verify" in w for w in r["warnings"]), r["warnings"])

    def test_signatures_are_read_from_the_updates_own_probe(self):
        kernel = self.secure_boot_machine()
        r = self.checks(probe=engine.parse_probe(self.probe_says(sbctl=f"✓ {kernel} is signed")))
        self.assertIn("подпись Secure Boot в порядке", r["ok"])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(self.probed, [])               # nothing left to look at now

    # ── boot files

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

    def test_a_readable_image_is_looked_into(self):
        kernel = self.boot_file("vmlinuz-linux")
        self.linux(kernel, self.boot_file("initramfs-linux.img"))
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs на месте", r["ok"])
        self.assertEqual(r["verdict"], 0)

    def test_root_only_initramfs_is_not_a_problem(self):
        kernel = self.boot_file("vmlinuz-linux")
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.linux(kernel, image)
        if os.access(image, os.R_OK):
            self.skipTest("running as root: every file is readable")
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs на месте", r["ok"])

    def test_a_root_only_image_built_for_another_kernel_is_found_by_the_probe(self):
        kernel = self.boot_file("vmlinuz-linux")
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.linux(kernel, image)
        if os.access(image, os.R_OK):
            self.skipTest("running as root: every file is readable")
        self.probe = self.probe_says(images=[(image, 0, ["7.2.10-arch1-1"])])    # sudo asks nothing here
        r = self.checks()
        self.assertTrue(any("собран не под ядро" in p and image in p for p in r["problems"]), r["problems"])
        self.assertEqual(self.fix_keys(r), ["initramfs"])
        self.assertEqual(r["fixes"][0]["argv"], ["sudo", "mkinitcpio", "-P"])

    def test_missing_kernel_file_is_a_problem_with_a_fix(self):
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.linux(f"{self.tmp}/gone", image)
        r = self.checks()
        self.assertTrue(any("НЕ перезагружайся" in p and "/gone" in p for p in r["problems"]), r["problems"])
        self.assertEqual([(f["key"], f["argv"], f["label"]) for f in r["fixes"]],
                         [("kernel", ["sudo", "pacman", "-S", "linux"],
                           "переустановить ядро (linux): вернёт файл ядра и initramfs")])
        # the runner keeps the kernel's boot files and checks them after pacman
        self.assertEqual(r["fixes"][0]["args"], ["r:linux", f"k:{self.tmp}/gone", f"i:{KVER}:1:{image}"])

    def test_the_kernel_comes_back_as_it_was_installed(self):
        image = self.boot_file("initramfs-linux.img", mode=0o000)
        self.linux(f"{self.tmp}/gone", image)
        self.after = {"linux": "7.2.11.arch1-1"}
        # pacman's cache has the installed version: that very file, whatever the repository has meanwhile
        os.makedirs(engine.PACMAN_CACHE)
        for name in ("linux-7.2.11.arch1-1-x86_64.pkg.tar.zst", "linux-7.2.11.arch1-1-x86_64.pkg.tar.zst.sig",
                     "linux-7.2.10.arch1-1-x86_64.pkg.tar.zst", "linux-headers-7.2.11.arch1-1-x86_64.pkg.tar.zst"):
            write(os.path.join(engine.PACMAN_CACHE, name), "", 0o644)
        self.newer = {"linux": "7.2.12.arch1-1"}
        r = self.checks(before=dict(self.after))
        self.assertEqual(r["fixes"][0]["argv"], ["sudo", "pacman", "-U", os.path.join(
            engine.PACMAN_CACHE, "linux-7.2.11.arch1-1-x86_64.pkg.tar.zst")])
        # not in the cache: from the repository, unless that would update the kernel alone
        shutil.rmtree(engine.PACMAN_CACHE)
        self.assertEqual(self.checks(before=dict(self.after))["fixes"], [])
        self.newer = {}
        self.assertEqual(self.checks(before=dict(self.after))["fixes"][0]["argv"], ["sudo", "pacman", "-S", "linux"])

    def test_an_empty_image_is_rebuilt(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img", data=b""))
        r = self.checks()
        self.assertEqual(r["verdict"], 2)
        self.assertEqual(self.fix_keys(r), ["initramfs"])

    def test_failed_mkinitcpio_in_the_output_is_rebuilt(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        r = self.checks(failures={"Initcpio image generation FAILED"})
        self.assertIn("mkinitcpio не собрал initramfs: «Initcpio image generation FAILED» — НЕ перезагружайся "
                      "(сначала: sudo mkinitcpio -P)", r["problems"])
        self.assertEqual(self.fix_keys(r), ["initramfs"])
        # what mkinitcpio says when it went on after errors and wrote the image all the same
        for said in ("errors were encountered during the build", "No modules were added to the image"):
            r = self.checks(failures={said})
            self.assertEqual(r["verdict"], 2, said)
            self.assertTrue(any(said in p and "НЕ перезагружайся" in p for p in r["problems"]), r["problems"])
            self.assertEqual(self.fix_keys(r), ["initramfs"])

    def test_the_chromebook_that_did_not_boot(self):
        # 2026-11-02: the disk filled up during the update. depmod could not write the new kernel's module list,
        # mkinitcpio then found no modules and wrote an initramfs without any; pacman only said "command failed".
        # The look into the image (as root, after the update) saw the kernel's folder and called it fine. The
        # next update, in the same boot, said "reboot when convenient"; the machine did not boot.
        self.patch(engine, "running_kernel", lambda: "7.2.10-arch1-1")
        image = self.boot_file("initramfs-linux.img", modules=False)
        self.linux(self.boot_file("vmlinuz-linux"), image, index=False)
        self.after = {"linux": "7.2.14.arch1-1"}
        failures = {"No space left on device", "error: command failed to execute correctly", "depmod: FATAL",
                    "No modules were added to the image", "errors were encountered during the build"}
        self.new_run()
        first = self.checks(before={"linux": "7.2.11.arch1-1"}, failures=failures, carry=True)
        self.assertEqual(first["verdict"], 2)
        self.assertIn(engine.DISK_FULL, first["problems"])
        kinds = [i["kind"] for i in first["boot"]]
        self.assertIn("depmod", kinds)
        self.assertIn("initcpio", kinds)
        self.assertTrue(any(i["kind"] == "image" and "нет модулей ядра" in i["text"] for i in first["boot"]))
        self.assertEqual(kinds.count("depmod"), 1)          # the module list missing says what depmod failed on
        self.assertEqual(self.fix_keys(first), ["space", "depmod", "initramfs"])
        self.assertEqual(first["fixes"][1]["argv"], ["sudo", "depmod", KVER])
        self.assertFalse(any(o.startswith("ядро и initramfs") for o in first["ok"]), first["ok"])
        self.assertFalse(any("перезагрузись" in n for n in first["notes"]), first["notes"])
        self.assertTrue(any("только когда проблемы выше будут исправлены" in n for n in first["notes"]))
        # opened again and updated again, in the same boot: nothing new to install, still red
        self.new_run()
        self.after = {"linux": "7.2.14.arch1-1"}
        second = self.checks(before=dict(self.after), carry=True)
        self.assertEqual(second["verdict"], 2)
        self.assertFalse(any("перезагрузись" in n for n in second["notes"]), second["notes"])
        self.assertEqual(engine.unresolved()["log"], second["log"])
        # depmod and mkinitcpio -P went through (F): the next look finds nothing more
        write(engine.module_index(KVER), "index", 0o644)
        os.remove(image + ".nomods")
        self.assertEqual(engine.recheck_boot(second["log"]), [])
        self.assertTrue(engine.mark_resolved(second["log"]))
        self.assertIsNone(engine.unresolved())

    def test_a_root_only_image_without_modules_is_found_by_the_probe(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        probe = self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])],
                                without_modules=[image])
        r = self.checks(probe=engine.parse_probe(probe))
        self.assertTrue(any(f"в {image} нет модулей ядра" in p for p in r["problems"]), r["problems"])
        self.assertEqual(self.fix_keys(r), ["initramfs"])
        # a kernel without loadable modules boots without them: nothing to miss there
        os.remove(os.path.join(engine.MODULES_DIR, KVER, "modules.order"))
        self.assertEqual(self.checks(probe=engine.parse_probe(probe))["problems"], [])

    def test_depmod_failing_on_lists_already_there_is_kept_until_they_are_written_again(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        index = engine.module_index(KVER)
        os.utime(index, (1, 1))                           # from before the update
        self.new_run()
        first = self.checks(failures={"depmod: FATAL"}, carry=True)
        self.assertEqual(self.fix_keys(first), ["depmod", "initramfs"])
        self.assertTrue(any("depmod: FATAL" in p for p in first["problems"]), first["problems"])
        self.new_run()
        second = self.checks(carry=True)
        self.assertTrue(any(p.startswith("не исправлено после обновления") and "depmod" in p
                            for p in second["problems"]), second["problems"])
        os.utime(index)                                   # sudo depmod went through
        self.new_run()
        self.assertEqual(self.checks(carry=True)["problems"], [])

    def test_a_full_disk_is_a_problem(self):
        r = self.checks(failures={"No space left on device"})
        self.assertEqual((r["verdict"], r["problems"]), (2, [engine.DISK_FULL]))
        self.assertEqual(self.fix_keys(r), ["space"])          # F frees room first; the update is to run again

    def test_a_summary_a_full_disk_did_not_let_be_written_still_warns(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img", data=b""))
        self.new_run()

        def full(path, data):
            raise OSError(28, "No space left on device")
        self.patch(engine, "write_json", full)
        r = self.checks(failures={"No space left on device"})
        self.assertFalse(os.path.exists(engine.summary_path(r["log"])))
        bad = engine.unresolved()
        self.assertEqual(bad["log"], r["log"])
        self.assertIn(engine.DISK_FULL, bad["problems"])
        self.assertTrue(any("пустой" in p for p in bad["problems"]), bad["problems"])

    def test_after_a_fix_that_failed_nothing_found_before_is_dropped(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        r = self.checks(failures={"errors were encountered during the build"})
        self.assertEqual(self.fix_keys(r), ["initramfs"])
        # mkinitcpio -P failed again (exit 1); the look right after it finds the image whole
        out = os.path.join(self.tmp, "probe.out")
        write(out, self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])]), 0o600)
        left = engine.recheck_boot(r["log"], probe=out, failed=True)
        self.assertTrue(any("errors were encountered" in p for p in left), left)
        self.assertFalse(engine.mark_resolved(r["log"]))
        self.assertEqual(engine.unresolved()["log"], r["log"])
        self.assertEqual([f["key"] for f in self.summary(r)["fixes"]], ["initramfs"])
        # it went through the second time
        self.assertEqual(engine.recheck_boot(r["log"], probe=out), [])
        self.assertTrue(engine.mark_resolved(r["log"]))

    def test_a_kernel_partition_to_flash_has_no_fix_here(self):
        r = self.checks(failures={"You can do this later by running: # dd if=/boot/vmlinux.kpart of=/dev/mmcblk0p1"})
        self.assertTrue(any("dd if=/boot/vmlinux.kpart of=/dev/mmcblk0p1" in p for p in r["problems"]))
        self.assertEqual(r["fixes"], [])

    def test_a_stale_keyring_is_no_boot_problem(self):
        r = self.checks(failures={"invalid or corrupted package (PGP signature)"})
        self.assertTrue(any("keyring" in p for p in r["problems"]), r["problems"])
        self.assertEqual(r["fixes"], [])

    def test_an_unmounted_boot_is_mounted_first(self):
        self.patch(engine, "unmounted_boot", lambda: ["/boot"])
        self.kernel("linux", KVER)
        self.dkms = "nvidia/615.78: added\n"
        r = self.checks()
        self.assertEqual(self.fix_keys(r), ["mount", "dkms"])        # no preset here: no initramfs to rebuild
        self.assertEqual(r["fixes"][0]["argv"], ["sudo", "mount", "/boot"])
        self.assertEqual(r["fixes"][1]["argv"], ["sudo", "dkms", "autoinstall", "-k", KVER])
        self.assertIn("NVIDIA", r["fixes"][1]["label"])

    def test_a_dkms_build_is_followed_by_the_initramfs(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        self.dkms = "nvidia/615.78: added\n"
        self.assertEqual(self.fix_keys(self.checks()), ["dkms", "initramfs"])

    def test_nothing_to_fix_no_fixes(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        r = self.checks(exitstatus=5)                     # pacman failed: a problem, but none of booting
        self.assertEqual(r["verdict"], 2)
        self.assertEqual(r["fixes"], [])

    def test_a_boot_closed_to_users_is_not_called_missing(self):
        # A Chromebook updated gnupg and brave and was told its kernel was gone and not to reboot.
        self.linux(*self.closed_boot("vmlinuz-linux", "initramfs-linux.img"))
        self.pacman_log("[ALPM] upgraded gnupg (2.4.9-3 -> 2.4.9-4)\n")
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs этим обновлением не менялись", r["ok"])
        self.assertNotIn("ядро и initramfs на месте", r["ok"])          # nothing was looked at

    def test_a_closed_boot_rebuilt_by_mkinitcpio_is_judged_by_its_output(self):
        self.linux(*self.closed_boot("vmlinuz-linux", "initramfs-linux.img"))
        for said in ("==> Initcpio image generation successful", "==> Image generation successful"):   # v38 on, v37
            self.pacman_log(f"[ALPM] running '90-mkinitcpio-install.hook'...\n[ALPM-SCRIPTLET] {said}\n")
            r = self.checks()
            self.assertEqual(r["problems"], [])
            self.assertIn("initramfs собран заново (по выводу mkinitcpio)", r["ok"])
        self.pacman_log("[ALPM] running '90-mkinitcpio-install.hook'...\n")   # no word on the result
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertTrue(any("sudo ls -l /boot" in w for w in r["warnings"]), r["warnings"])
        # one of two presets went wrong: not built whole
        self.pacman_log("[ALPM-SCRIPTLET] ==> Initcpio image generation successful\n"
                        "[ALPM-SCRIPTLET] ==> WARNING: errors were encountered during the build. The image may not "
                        "be complete.\n")
        self.assertNotIn("initramfs собран заново (по выводу mkinitcpio)", self.checks()["ok"])

    def test_a_closed_boot_is_looked_into_through_sudo_when_it_asks_nothing(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, None)])     # the image is really gone
        r = self.checks()
        self.assertTrue(any("НЕ перезагружайся" in p and image in p for p in r["problems"]), r["problems"])
        self.assertEqual(self.probed[0], ["0", "2", kernel, image, "1", image])
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])])
        r = self.checks()
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs на месте", r["ok"])

    def test_a_closed_boot_is_looked_into_by_the_updates_own_probe(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        probe = engine.parse_probe(self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])]))
        r = self.checks(probe=probe)
        self.assertEqual(r["problems"], [])
        self.assertIn("ядро и initramfs на месте", r["ok"])
        self.assertEqual(self.probed, [])

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

    def test_only_the_machines_own_battery_counts(self):
        self.supply("hidpp_battery_0", type="Battery", scope="Device", capacity=18, status="Discharging")
        self.supply("AC", type="Mains", online=1)
        self.assertIsNone(engine.battery())
        self.supply("BAT0", type="Battery", capacity=64, status="Charging")
        self.assertEqual(engine.battery(), (64, False))

    def test_no_dkms_no_nvidia_lines(self):
        self.kernel("linux", KVER)
        r = self.checks()
        self.assertFalse(any("NVIDIA" in line or "DKMS" in line for line in r["problems"] + r["ok"]))

    # ── what makes the verdict yellow, and what is only a note

    def test_a_kernel_update_with_everything_in_place_stays_green(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        self.patch(engine, "running_kernel", lambda: "7.2.10-arch1-1")
        self.after = {"linux": "7.2.11.arch1-1", "mesa": "1:26.0-1"}
        r = self.checks(before={"linux": "7.2.10.arch1-1", "mesa": "1:25.9-1"})
        self.assertEqual((r["verdict"], r["warnings"], r["problems"]), (0, [], []))
        self.assertIn("обновилось ядро — перезагрузись, когда будет удобно (обновились: linux, mesa)", r["notes"])
        self.assertTrue(r["reboot"])

    def test_running_kernel_replaced_is_a_note(self):
        self.kernel("linux", "0.0.1-not-the-running-one")
        r = self.checks()
        self.assertIn("обновилось ядро — перезагрузись, когда будет удобно", r["notes"])
        self.assertFalse(any("ядро" in w for w in r["warnings"]), r["warnings"])

    def test_without_known_kernels_arch_updates_reboot_note_counts(self):
        r = self.checks(notes={"reboot"})
        self.assertTrue(any("перезагруз" in n for n in r["notes"]), r["notes"])
        self.assertTrue(r["reboot"])
        self.assertEqual(r["verdict"], 0)

    def test_orphans_from_before_are_a_note_new_ones_a_warning(self):
        self.orphans = ["old-lib"]
        r = self.checks(notes={"orphans"}, orphans_before={"old-lib"})
        self.assertEqual((r["verdict"], r["notes"]), (0, ["сироты: 1 — посмотреть: pacman -Qdtq"]))
        self.orphans = ["old-lib", "python-gone"]
        r = self.checks(notes={"orphans"}, orphans_before={"old-lib"})
        self.assertEqual(r["warnings"], ["новый пакет-сирота после обновления: python-gone — не удалял, "
                                         "посмотреть: pacman -Qdtq"])
        self.orphans = ["old-lib", "python-gone", "lib32-gone"]
        r = self.checks(orphans_before={"old-lib"})
        self.assertTrue(r["warnings"][0].startswith("новые пакеты-сироты после обновления: lib32-gone, python-gone"))
        self.orphans = []
        self.assertEqual(self.checks(orphans_before={"old-lib"})["notes"], [])     # removed meanwhile

    def test_orphans_not_known_from_before_are_still_said(self):
        self.orphans = ["old-lib"]
        self.assertTrue(any("сироты" in w for w in self.checks()["warnings"]))

    def test_pacnew_files_from_before_are_a_note_new_ones_a_warning(self):
        self.pacdiff = ["/etc/pacman.conf.pacnew"]
        r = self.checks(notes={"pacnew"})
        self.assertEqual((r["verdict"], r["notes"]),
                         (0, [".pacnew от прошлых обновлений: 1 — посмотреть: pacdiff -o"]))
        made = self.boot_file("locale.gen.pacnew")
        self.pacman_log(f"[2026-11-03T10:00:00+0300] [ALPM] warning: {made[:-7]} installed as {made}\n")
        self.pacdiff = ["/etc/pacman.conf.pacnew", made]
        r = self.checks(notes={"pacnew"})
        self.assertEqual(len(r["warnings"]), 1)
        self.assertTrue(r["warnings"][0].startswith(f"новый .pacnew от этого обновления: {made} — "), r["warnings"])
        os.remove(made)                                   # merged meanwhile (by hand, in manual mode)
        self.pacdiff = ["/etc/pacman.conf.pacnew"]
        self.assertEqual(self.checks(notes={"pacnew"})["warnings"], [])

    def test_pacnew_files_are_a_warning_when_nobody_can_tell_which_update_made_them(self):
        self.patch(engine, "PACMAN_LOG", os.path.join(self.tmp, "gone.log"))
        self.pacdiff = ["/etc/pacman.conf.pacnew"]
        self.assertTrue(any(".pacnew" in w for w in self.checks()["warnings"]))

    def test_chromebook_audio_is_a_note(self):
        self.patch(engine, "is_chromebook", lambda: True)
        self.after = {"sof-firmware": "2"}
        r = self.checks(before={"sof-firmware": "1"})
        self.assertTrue(any("chromebook-linux-audio" in n for n in r["notes"]), r["notes"])
        self.assertEqual(r["warnings"], [])

    def test_services_waiting_and_services_left_for_the_reboot(self):
        responder = engine.Responder(child=None, stop_on_news=True, manual=False)
        responder._dm, responder._own = "", ""
        responder.tail = services_tail("cups.service", "dbus-broker.service", "sshd.service")
        responder.decide()                                # the setting is off: Enter
        r = engine.system_checks({}, 0, responder, 0, carry=False)
        self.assertEqual(r["warnings"], ["сервисы ждут перезапуска: cups, sshd"])
        self.assertEqual(r["notes"], ["dbus-broker перезапустится после перезагрузки — на ходу его не трогаю"])
        self.assertEqual(r["services"], {"listed": ["cups.service", "dbus-broker.service", "sshd.service"],
                                         "restarted": [], "skipped": ["cups.service", "dbus-broker.service",
                                                                      "sshd.service"],
                                         "waiting": ["cups.service", "sshd.service"],
                                         "later": ["dbus-broker.service"], "failed": []})
        # the UI's key restarts the waiting ones: the summary follows, the verdict turns green
        data = engine.services_restarted(r["log"], ["cups.service", "sshd.service"])
        self.assertEqual((data["verdict"], data["warnings"]), (0, []))
        self.assertIn("перезапущены сервисы: cups, sshd", data["ok"])
        self.assertEqual(self.summary(r)["services"]["waiting"], [])

    def test_a_service_that_did_not_restart_is_a_warning_not_a_problem(self):
        responder = engine.Responder(child=None, stop_on_news=True, manual=False, restart_services=True)
        responder._dm, responder._own = "", ""
        responder.tail = services_tail("cups.service")
        responder.decide()
        responder.watch_services("==> ERROR: An error has occurred during the service(s) restart\n")
        self.failed = ["cups.service"]
        r = engine.system_checks({}, 11, responder, 0, carry=False)      # arch-update's code for it
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["warnings"], ["не перезапустился сервис: cups — посмотри: systemctl status cups.service"])

    def test_code_11_alone_is_a_warning(self):
        r = self.checks(exitstatus=11)
        self.assertEqual((r["verdict"], r["problems"]), (1, []))
        self.assertTrue(any("код 11" in w for w in r["warnings"]))

    # ── the summary next to the journal

    def test_every_result_leaves_a_summary(self):
        self.kernel("linux", KVER)
        self.dkms = "nvidia/615.78: added\n"
        r = self.checks()
        s = self.summary(r)
        for key in ("verdict", "changed", "added", "removed", "problems", "warnings", "notes", "ok", "exitstatus",
                    "started", "mode", "began", "ended", "boot_id", "services", "fixes"):
            self.assertIn(key, s)
        self.assertEqual((s["verdict"], s["mode"], s["started"], s["boot_id"]), (2, "auto", True, engine.boot_id()))
        self.assertEqual(s["problems"], r["problems"])
        self.assertEqual(s["fixes"], r["fixes"])
        self.assertEqual(set(s["services"]), {"listed", "restarted", "skipped", "waiting", "later", "failed"})
        self.assertEqual(engine.last_update()["log"], r["log"])
        self.assertEqual(engine.unresolved()["log"], r["log"])
        self.new_run()
        r = engine.not_started("обновление отменено — система не менялась", 1)
        s = self.summary(r)
        self.assertEqual((s["verdict"], s["started"], s["head"]), (1, False, "Обновление не начиналось"))

    def test_summaries_go_with_their_journals(self):
        logs = engine.LOG.dir
        for i in range(engine.KEEP_LOGS + 2):
            for ext in (".log", ".raw", ".json"):
                write(os.path.join(logs, f"2000-01-01_00-00-{i:02d}{ext}"), "{}", 0o644)
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        engine.LOG = engine.RunLog()
        self.assertFalse(os.path.exists(os.path.join(logs, "2000-01-01_00-00-00.json")))
        self.assertFalse(os.path.exists(os.path.join(logs, "2000-01-01_00-00-02.json")))
        self.assertTrue(os.path.exists(os.path.join(logs, "2000-01-01_00-00-03.json")))
        self.assertEqual(len(glob.glob(os.path.join(logs, "20*.log"))), engine.KEEP_LOGS)

    # ── after a red verdict: the fix, the second look, the warning that stays

    def test_a_fix_is_checked_again_and_the_summary_follows(self):
        image = self.boot_file("initramfs-linux.img", data=b"")
        self.linux(self.boot_file("vmlinuz-linux"), image)
        r = self.checks()
        self.assertEqual(engine.unresolved()["log"], r["log"])
        with open(image, "wb") as f:                     # sudo mkinitcpio -P went through
            f.write(b"x" * 64)
        self.assertEqual(engine.recheck_boot(r["log"]), [])
        s = self.summary(r)
        self.assertEqual((s["problems"], s["fixes"], s["boot"], s["verdict"]), ([], [], [], 0))
        self.assertIn("rechecked", s)
        self.assertIsNone(engine.unresolved())
        self.assertTrue(engine.mark_resolved(r["log"]))
        self.assertIn("resolved", self.summary(r))
        with open(r["log"], encoding="utf-8") as f:
            journal = f.read()
        self.assertIn("ПЕРЕПРОВЕРКА] проблем с загрузкой не нашёл", journal)

    def test_what_nobody_can_look_at_again_stays_until_a_probe_sees_it(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, 0)])     # sudo asked nothing then
        r = self.checks()
        empty = [p for p in r["problems"] if image in p]
        self.assertEqual(len(empty), 1)
        self.probe = ""                                   # and asks now: nobody can look
        self.assertEqual(engine.recheck_boot(r["log"]), empty)
        self.assertEqual(engine.unresolved()["log"], r["log"])
        out = os.path.join(self.tmp, "probe.out")         # what the fix's own probe saw, in its terminal
        write(out, self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])]), 0o600)
        self.assertEqual(engine.recheck_boot(r["log"], probe=out), [])
        self.assertIsNone(engine.unresolved())

    def test_a_problem_found_before_is_kept_by_the_next_update_until_ruled_out(self):
        # The Chromebook: told not to reboot, the updater was opened again and the next update said only
        # "something to read" — it could not look into the closed /boot. Rebooted, it did not boot.
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        self.new_run()
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, 0)])
        first = self.checks(carry=True)
        self.assertEqual(first["verdict"], 2)
        self.new_run()
        self.probe = ""                                   # now sudo asks for its password: nobody can look
        self.pacman_log("[ALPM] upgraded gnupg (2.4.9-3 -> 2.4.9-4)\n")
        second = self.checks(carry=True)
        kept = [p for p in second["problems"] if p.startswith("не исправлено после обновления в 01.01 00:00: ")]
        self.assertEqual(len(kept), 1, second["problems"])
        self.assertIn(image, kept[0])
        self.assertNotIn("ядро и initramfs этим обновлением не менялись", second["ok"])
        self.assertEqual(self.fix_keys(second), ["initramfs"])
        self.assertEqual(engine.unresolved()["log"], second["log"])
        self.new_run()
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, 64)], images=[(image, 0, [KVER])])
        third = self.checks(carry=True)
        self.assertEqual(third["problems"], [])
        self.assertIsNone(engine.unresolved())

    def test_a_failed_mkinitcpio_is_kept_until_an_image_is_built_again(self):
        self.linux(*self.closed_boot("vmlinuz-linux", "initramfs-linux.img"))
        self.new_run()
        first = self.checks(failures={"Initcpio image generation FAILED"}, carry=True)
        self.assertEqual(first["verdict"], 2)
        self.new_run()
        second = self.checks(carry=True)
        self.assertTrue(any("не исправлено" in p and "Initcpio" in p for p in second["problems"]))
        self.new_run()
        self.pacman_log("[ALPM] running '90-mkinitcpio-install.hook'...\n"
                        "[ALPM-SCRIPTLET] ==> Initcpio image generation successful\n")
        third = self.checks(carry=True)
        self.assertEqual(third["problems"], [])

    def test_an_image_built_for_another_kernel_stays_until_somebody_looks_inside(self):
        kernel = self.boot_file("vmlinuz-linux")
        image = self.boot_file("initramfs-linux.img", mode=0o000)       # root-only: its size is all the user sees
        self.linux(kernel, image)
        if os.access(image, os.R_OK):
            self.skipTest("running as root: every file is readable")
        self.new_run()
        self.probe = self.probe_says(images=[(image, 0, ["7.2.10-arch1-1"])])
        self.assertEqual(self.checks(carry=True)["verdict"], 2)
        self.new_run()
        self.probe = ""
        second = self.checks(carry=True)
        self.assertTrue(any(p.startswith("не исправлено") and "собран не под ядро" in p for p in second["problems"]))
        self.assertFalse(any(o.startswith("ядро и initramfs") for o in second["ok"]), second["ok"])
        self.new_run()
        self.probe = self.probe_says(images=[(image, 0, [KVER])])
        third = self.checks(carry=True)
        self.assertEqual(third["problems"], [])
        self.assertIn("ядро и initramfs на месте", third["ok"])

    # ── F (arch-update-ui's fix_problems), the plain terminal it runs the fixes in stubbed: a fix that ended with an
    #    error fixed nothing and the warning stays, whatever the look right after it says; one that went through ends
    #    the warning only when no problem is left, one that is not about booting included.

    def fix_key(self, res, status, seen=()):
        if not hasattr(self, "ui"):
            path = os.path.join(ROOT, "bin", "arch-update-ui")
            self.patch(sys, "argv", [path])
            loader = importlib.machinery.SourceFileLoader("arch_update_ui", path)
            self.ui = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
            loader.exec_module(self.ui)
            self.said = []
            self.patch(self.ui, "back_to_canvas", lambda app, lines: self.said.extend(text for text, _, _ in lines))
            self.patch(self.ui, "say_plain", lambda *a, **k: None)
        plan = engine.fix_commands(res)

        def terminal(app, argv, intro):
            # what the catalogue's runner writes: every fix done and checked, or the first one put back
            if "arch-update-fix" in argv and status in (0, fixes.ROLLED_BACK):
                with open(argv[argv.index("arch-update-fix") + 1], "w", encoding="utf-8") as f:
                    for i, fix in enumerate(plan):
                        f.write(f"{fix['key']} {'ok' if status == 0 else 'rolledback' if i == 0 else 'skipped'}\n")
            return status, set(seen)
        self.patch(self.ui, "run_in_terminal", terminal)
        self.said.clear()
        return self.ui.fix_problems(types.SimpleNamespace(s=None), res, plan)

    def test_a_fix_that_failed_fixed_nothing(self):
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img"))
        r = self.checks(failures={"errors were encountered during the build"})
        res = dict(r)
        # mkinitcpio -P exited 1 and wrote the image all the same: the look finds it whole, it is not fixed
        text, colour = self.fix_key(res, 1, {"errors were encountered during the build"})
        self.assertEqual(colour, self.ui.BAD)
        self.assertTrue(text.startswith("НЕ перезагружайся: исправление не прошло"), text)
        self.assertTrue(any("НЕ перезагружайся" in s for s in self.said), self.said)
        self.assertEqual(engine.unresolved()["log"], r["log"])
        self.assertNotIn("resolved", self.summary(r))
        self.assertEqual([f["key"] for f in self.summary(r)["fixes"]], ["initramfs"])
        # a full disk: said so
        self.fix_key(res, 1, {"No space left on device"})
        self.assertTrue(any("нет места" in s for s in self.said), self.said)
        # it went through
        text, colour = self.fix_key(res, 0)
        self.assertEqual((colour, engine.unresolved()), (self.ui.GOOD, None))
        self.assertIn("resolved", self.summary(r))

    def test_a_problem_not_about_booting_keeps_the_warning(self):
        lock = os.path.join(self.tmp, "db.lck")
        write(lock, "", 0o644)
        self.patch(engine, "PACMAN_LOCK", lock)           # pacman was cut off: its lock is left behind
        self.linux(self.boot_file("vmlinuz-linux"), self.boot_file("initramfs-linux.img", data=b""))
        r = self.checks()
        self.assertEqual(len(r["problems"]), 2)
        write(os.path.join(self.tmp, "initramfs-linux.img"), "x" * 64, 0o644)      # mkinitcpio -P went through
        text, colour = self.fix_key(dict(r), 0)
        self.assertEqual(colour, self.ui.WARN)
        self.assertIn("остался замок pacman", text)
        self.assertEqual(engine.unresolved()["log"], r["log"])
        self.assertNotIn("resolved", self.summary(r))

    def test_the_demo_carries_nothing(self):
        kernel, image = self.closed_boot("vmlinuz-linux", "initramfs-linux.img")
        self.linux(kernel, image)
        self.new_run()
        self.probe = self.probe_says(sizes=[(kernel, 64), (image, 0)])
        self.checks(carry=True)
        self.new_run()
        self.probe = ""                                   # nobody can look: only carrying would keep it
        self.assertEqual(engine.unresolved()["verdict"], 2)
        self.assertEqual(self.checks(carry=False)["problems"], [])


class Summaries(Patching):
    """The menu's warning: the newest update of this boot, if it ended red and nobody fixed it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env(ARCH_UPDATE_AUTO_LOGDIR=self.dir)
        self.boot = os.path.join(self.dir, "boot_id")
        write(self.boot, "boot-a\n", 0o644)
        self.patch(engine, "BOOT_ID", self.boot)
        self.stat = os.path.join(self.dir, "stat")       # this boot began on 2026-11-03 at 09:00, local time
        write(self.stat, f"cpu  1 2 3\nbtime {int(time.mktime((2026, 11, 3, 9, 0, 0, 0, 0, -1)))}\n", 0o644)
        self.patch(engine, "PROC_STAT", self.stat)

    def update(self, stamp, verdict, boot="boot-a", started=True, **more):
        data = dict(verdict=verdict, started=started, boot_id=boot, problems=["p"] if verdict == 2 else [], **more)
        write(os.path.join(self.dir, stamp + ".json"), json.dumps(data), 0o644)
        write(os.path.join(self.dir, stamp + ".log"), "", 0o644)
        return os.path.join(self.dir, stamp + ".log")

    def journal(self, stamp, text):
        """A journal without a summary: an older version's, or one whose summary a full disk did not let be written."""
        path = os.path.join(self.dir, stamp + ".log")
        write(path, text, 0o644)
        return path

    def test_boot_id(self):
        self.assertEqual(engine.boot_id(), "boot-a")
        self.patch(engine, "BOOT_ID", os.path.join(self.dir, "nope"))
        self.assertEqual(engine.boot_id(), "")

    def test_the_newest_red_update_of_this_boot_is_unresolved(self):
        self.assertIsNone(engine.last_update())
        self.assertIsNone(engine.unresolved())
        log = self.update("2026-11-03_10-00-00", 2)
        self.assertEqual(engine.last_update()["log"], log)
        self.assertEqual(engine.unresolved()["log"], log)
        write(self.boot, "boot-b\n", 0o644)               # rebooted since
        self.assertIsNone(engine.unresolved())

    def test_a_run_that_did_not_start_does_not_hide_it(self):
        log = self.update("2026-11-03_10-00-00", 2)
        self.update("2026-11-03_11-00-00", 1, started=False)          # declined: nothing changed
        self.assertEqual(engine.unresolved()["log"], log)
        red = self.update("2026-11-03_12-00-00", 2, started=False)    # /boot not mounted: red itself
        self.assertEqual(engine.unresolved()["log"], red)

    def test_a_later_update_that_went_well_ends_it(self):
        self.update("2026-11-03_10-00-00", 2)
        self.update("2026-11-03_11-00-00", 1)
        self.assertIsNone(engine.unresolved())

    def test_marked_resolved_it_is_not(self):
        log = self.update("2026-11-03_10-00-00", 2)
        # a problem still there (a pacman lock left behind, a failed hook): it stays a problem
        self.assertFalse(engine.mark_resolved(log))
        self.assertEqual(engine.unresolved()["log"], log)
        engine._update_summary(log, lambda d: d.update(problems=[]))      # recheck_boot found it gone
        self.assertTrue(engine.mark_resolved(log))
        self.assertIsNone(engine.unresolved())
        self.assertIn("resolved", engine.last_update())
        with open(log, encoding="utf-8") as f:
            self.assertIn("ИСПРАВЛЕНО]", f.read())
        self.assertFalse(engine.mark_resolved(os.path.join(self.dir, "2026-11-03_09-00-00.log")))

    def test_without_a_summary_the_journal_says_it(self):
        # The Chromebook's disk was full: the summary could not be written, the journal was (its file was open).
        log = self.journal("2026-11-03_12-50-12", "[12:50:12 СТАРТ] arch-update, режим: автоответы\n"
                                                  "[12:57:54 КОНЕЦ] arch-update завершился, код 0\n"
                                                  "[12:57:54 ПАКЕТЫ] обновлены: linux 7.2.11.arch1-1→7.2.14.arch1-1\n"
                                                  "[12:57:55 ПРОБЛЕМА] в выводе: «No space left on device»\n"
                                                  "[12:57:55 ВНИМАНИЕ] есть пакеты-сироты\n"
                                                  "\n══════ /var/log/pacman.log за это обновление ══════\n"
                                                  "[12:57:56 ПРОБЛЕМА] what a hook printed\n")
        bad = engine.unresolved()
        self.assertEqual((bad["log"], bad["verdict"], bad["problems"]), (log, 2, ["в выводе: «No space left on device»"]))
        self.assertEqual((bad["began"], bad["boot"], bad["services"]), ("2026-11-03T12:50:12", [], {}))
        # a run without a result (cut off, or still running) says nothing either way
        self.journal("2026-11-03_13-00-00", "[13:00:00 СТАРТ] arch-update, режим: автоответы\n")
        self.assertEqual(engine.unresolved()["log"], log)
        self.assertIsNone(engine.unresolved(exclude=log))
        # the old version's code 11 is no problem any more
        self.journal("2026-11-03_13-10-00", "[13:15:00 ПАКЕТЫ] обновлены: —\n"
                                            "[13:15:00 ПРОБЛЕМА] arch-update завершился с кодом 11\n")
        self.assertIsNone(engine.unresolved())
        os.remove(os.path.join(self.dir, "2026-11-03_13-10-00.log"))
        # fixed with F and looked at again: its journal says so
        self.assertTrue(engine.mark_resolved(log))
        self.assertIsNone(engine.unresolved())

    def test_a_journal_from_before_this_boot_is_no_warning(self):
        self.journal("2026-11-03_08-30-00", "[08:30:00 ПАКЕТЫ] обновлены: —\n[08:30:01 ПРОБЛЕМА] нет /boot/x\n")
        self.assertIsNone(engine.unresolved())
        write(self.stat, "cpu 1 2 3\n", 0o644)            # no telling when this boot began
        self.journal("2026-11-03_10-00-00", "[10:00:00 ПАКЕТЫ] обновлены: —\n[10:00:01 ПРОБЛЕМА] нет /boot/x\n")
        self.assertIsNone(engine.unresolved())

    def test_a_summary_of_another_shape_breaks_nothing(self):
        log = self.update("2026-11-03_10-00-00", 2)
        write(log[:-4] + ".json", json.dumps({"verdict": 2, "boot_id": "boot-a", "problems": ["p", 1],
                                              "boot": [{"kind": "dkms", "kvers": 5, "text": "t"}, "x"],
                                              "services": {"waiting": 7, "later": ["x", 2]}, "warnings": "x",
                                              "fixes": [{"argv": "sudo"}]}), 0o644)
        bad = engine.unresolved()
        self.assertEqual((bad["problems"], bad["warnings"], bad["fixes"]), (["p"], [], []))
        self.assertEqual((bad["boot"], bad["services"]), ([{"kind": "dkms", "text": "t"}], {"later": ["x"]}))
        self.assertEqual(engine.fix_commands(bad), [])
        self.patch(engine, "boot_state", lambda *a, **k: ([], [], [], {}))    # nothing of this machine's own
        self.assertEqual(engine.recheck_boot(log), [])
        s = engine.read_json(log[:-4] + ".json")
        self.assertEqual((s["verdict"], s["problems"], s["boot"], s["warnings"]), (2, ["p"], [], []))

    def test_settings(self):
        self.assertEqual(engine.read_settings(), {})
        os.makedirs(engine.STATE_DIR, exist_ok=True)
        write(engine.SETTINGS, '{"restart_services": true, "reports": false}', 0o600)
        self.addCleanup(os.remove, engine.SETTINGS)
        self.assertEqual(engine.read_settings()["restart_services"], True)


class AutoSummary(Patching):
    """arch-update-auto prints the notes (dim) and the commands for what is left to do."""

    def test_notes_fixes_and_services_are_printed(self):
        path = os.path.join(ROOT, "bin", "arch-update-auto")
        self.patch(sys, "argv", [path])
        loader = importlib.machinery.SourceFileLoader("arch_update_auto", path)
        auto = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(auto)
        out = []
        self.patch(engine, "VIEW", types.SimpleNamespace(message=lambda msg, color="": out.append((msg, color))))
        self.patch(engine, "display_manager", lambda: "")
        self.patch(engine, "own_service", lambda: "")
        auto.print_summary({"changed": ["linux"], "added": [], "removed": [], "problems": ["нет /boot/x"],
                            "warnings": ["сервисы ждут перезапуска: cups"], "notes": ["сироты: 3 — посмотреть"],
                            "ok": [], "verdict": 2, "log": "/x.log", "reboot": True,
                            "fixes": [{"key": "initramfs", "label": "собрать initramfs заново",
                                       "argv": ["sudo", "mkinitcpio", "-P"]}],
                            "services": {"waiting": ["cups.service", "dbus.service"]}})
        text = "".join(msg for msg, _ in out)
        self.assertIn(("· сироты: 3 — посмотреть\n", engine.C_DIM), out)
        self.assertIn("  sudo mkinitcpio -P   # собрать initramfs заново\n", text)
        self.assertIn("Перезапустить сервисы сейчас: sudo systemctl restart -- cups.service\n", text)
        out.clear()
        auto.print_summary({"changed": [], "added": [], "removed": [], "problems": [], "warnings": [],
                            "notes": ["обновилось ядро — перезагрузись, когда будет удобно"], "ok": [],
                            "verdict": 0, "log": "/x.log", "reboot": True})
        text = "".join(msg for msg, _ in out)
        self.assertIn("✓ Всё прошло чисто — перезагрузись, когда будет удобно.", text)
        self.assertNotIn("перезагрузка не нужна", text)


class Stubs(Patching):
    """A stub arch-update (and whatever else a test needs) in PATH, no keyboard, nothing of the machine's own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.env(PATH=self.bin + os.pathsep + os.environ["PATH"], T=self.tmp,
                 ARCH_UPDATE_AUTO_LOGDIR=os.path.join(self.tmp, "logs"))
        saved_stdin = os.dup(0)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)                      # no keyboard
        os.close(null)
        self.addCleanup(os.close, saved_stdin)
        self.addCleanup(os.dup2, saved_stdin, 0)
        for name, value in (("unmounted_boot", lambda: []), ("sudo_asks_password", lambda: False),
                            ("installed_versions", lambda: {}), ("failed_units", lambda: []),
                            ("orphans", lambda: set()), ("display_manager", lambda: ""),
                            ("own_service", lambda: ""), ("battery", lambda: None)):
            self.patch(engine, name, value)

    def stub(self, name, text):
        write(os.path.join(self.bin, name), text)

    def run_update(self, view=None, **kw):
        class View(engine.TerminalView):
            def out(self, data):
                pass
        status, res, responder = engine.run_update(view or View(), cmd="fake-update", **kw)
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        with open(engine.LOG.path, encoding="utf-8") as f:
            return status, res, f.read()


class SpaceBeforeTheUpdate(Stubs):
    """The Chromebook of 2026-11-02: btrfs with snap-pac, pacman counted 7 MiB ("Net Upgrade Size") but the
    snapshot kept every replaced file, and the transaction ran out of room halfway. Now the room is looked at when
    pacman asks, counted as the snapshot makes it: too little, and pacman gets "n" — nothing changes, and F frees
    room."""

    # pacman's last words before its question, as on that Chromebook
    UPDATE = r"""#!/bin/bash
echo ":: Starting full system upgrade..."
echo "resolving dependencies..."
echo
echo "Packages (3) linux-7.2.14.arch1-1  linux-firmware-20261101-1  wine-11.2-1"
echo
echo "Total Download Size:    673.20 MiB"
echo "Total Installed Size:  1293.45 MiB"
echo "Net Upgrade Size:         7.15 MiB"
echo
read -rp ":: Proceed with installation? [Y/n] " ans
echo "answer=$ans"
[ "$ans" = n ] && exit 1
echo "installing"
"""

    def setUp(self):
        super().setUp()
        self.free = 1200 * 2 ** 20
        self.patch(engine, "mount_of", lambda path: ("btrfs", "/dev/mmcblk0p3"))
        self.patch(engine, "snapshot_hooks", lambda: ["05-snap-pac-pre.hook"])
        self.patch(engine.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=self.free, total=0, used=0))
        self.stub("fake-update", self.UPDATE)

    def test_too_little_room_with_a_snapshot_stops_before_anything_changes(self):
        status, res, log = self.run_update()
        self.assertIn("answer=n", log)
        self.assertNotIn("installing", log)
        self.assertEqual((res["started"], res["verdict"]), (False, 1))
        self.assertTrue(res["warnings"][0].startswith("мало места на диске: свободно 1,2 ГБ, а обновлению нужно "
                                                      "около 2,"), res["warnings"])
        self.assertIn("снимок snapper", res["warnings"][0])
        self.assertEqual([f["key"] for f in res["fixes"]], ["space"])
        need = int(res["fixes"][0]["args"][0])
        self.assertGreater(need, (1293 + 673) * 2 ** 20)            # every new file and the downloads
        self.assertIn("МЕСТО]", log)
        saved = engine.read_json(engine.summary_path(res["log"]))
        self.assertEqual(saved["space"]["need"], need)
        # F freed the room: the summary says so, F is gone, the update can run again
        self.free = 5 * 2 ** 30
        data = engine.space_freed(res["log"])
        self.assertEqual((data["warnings"], data.get("space")), ([], None))
        self.assertTrue(any(o.startswith("место освобождено") for o in data["ok"]))
        self.assertEqual(engine.fix_commands(data), [])

    def test_enough_room_or_no_snapshot_goes_on(self):
        self.free = 3 * 2 ** 30
        _, res, log = self.run_update()
        self.assertIn("answer=y", log)
        self.assertTrue(res["started"])
        self.free = 1200 * 2 ** 20
        self.patch(engine, "snapshot_hooks", lambda: [])             # pacman's own count holds without one
        _, res, log = self.run_update()
        self.assertIn("answer=y", log)
        self.patch(engine, "snapshot_hooks", lambda: ["05-snap-pac-pre.hook"])
        self.patch(engine, "mount_of", lambda path: ("ext4", "/dev/sda2"))
        _, res, log = self.run_update()
        self.assertIn("answer=y", log)

    def test_the_demo_never_checks(self):
        _, res, log = self.run_update(demo=True)
        self.assertIn("answer=y", log)

    def test_sizes_and_hooks(self):
        text = ("Packages (1) a-1\n\nTotal Installed Size:  1.50 GiB\n\n:: Proceed with installation? [Y/n] "
                "\nPackages (2) b-1 c-1\n\nTotal Download Size:  10.00 MiB\nTotal Installed Size:  20.00 MiB\n")
        self.assertEqual(engine.pacman_sizes(text), {"download": 10 * 2 ** 20, "installed": 20 * 2 ** 20})
        self.assertIsNone(engine.pacman_sizes("nothing here"))
        hooks = os.path.join(self.tmp, "hooks")
        etc = os.path.join(self.tmp, "etc-hooks")
        os.makedirs(hooks)
        write(os.path.join(hooks, "05-snap-pac-pre.hook"),
              "[Action]\nWhen = PreTransaction\nExec = /usr/share/libalpm/scripts/snap-pac pre\n", 0o644)
        write(os.path.join(hooks, "60-other.hook"), "[Action]\nWhen = PostTransaction\nExec = /bin/snapper\n",
              0o644)
        self.patch(engine, "HOOK_DIRS", (hooks, etc))
        self.assertEqual(self.REAL_HOOKS(), ["05-snap-pac-pre.hook"])
        os.makedirs(etc)
        os.symlink(os.devnull, os.path.join(etc, "05-snap-pac-pre.hook"))     # switched off by the user
        self.assertEqual(self.REAL_HOOKS(), [])

    def test_a_pacman_that_found_too_little_room_itself(self):
        self.stub("fake-update", "#!/bin/bash\necho 'error: Partition / too full: 12 blocks needed, 3 blocks free'\n"
                                 "echo 'error: not enough free disk space'\n"
                                 "echo 'error: failed to commit transaction (not enough free disk space)'\nexit 5\n")
        _, res, _ = self.run_update()
        self.assertEqual((res["started"], res["verdict"], res["warnings"]), (False, 1, [engine.FULL_BEFORE]))
        self.assertEqual([f["key"] for f in res["fixes"]], ["space"])

    def test_a_manual_run_is_warned_and_answers_itself(self):
        class View(engine.TerminalView):
            said = []

            def out(self, data):
                pass

            def message(self, msg, color=None):
                View.said.append(msg)
                if "ВНИМАНИЕ" in msg:
                    engine.RESPONDER.user_input(b"n\r")       # the user reads it and says no
        status, res, log = self.run_update(View(), manual=True)
        self.assertTrue(any("ВНИМАНИЕ: мало места на диске" in m for m in View.said), View.said)
        self.assertIn("ручной режим: предупредил", log)
        self.assertEqual((res["started"], [f["key"] for f in res["fixes"]]), (False, ["space"]))


SpaceBeforeTheUpdate.REAL_HOOKS = staticmethod(engine.snapshot_hooks)


class ServicesRun(Stubs):
    """The whole question through a real pty: the setting decides what arch-update gets typed."""

    # arch-update 4.4's restart_services.sh, as it shows (colours and all), with what systemctl would say
    UPDATE = r"""#!/bin/bash
b="\e[1m" bl="\e[1;34m" g="\e[1;32m" y="\e[1;33m" o="\e[0m"
echo -e "${bl}==>${o}${b} Services:\nThe following services require a post upgrade restart\n${o}"
i=1
for s in $UNITS; do echo "$i - $s"; i=$((i + 1)); done
echo
q='Select the service(s) to restart (e.g. 1 3 5), select 0 to restart them all or press "enter" '
q+='to continue without restarting the service(s):'
read -rp $"$(echo -e "${bl}->${o}${b} ${q}${o} ")" -a ans
echo
echo "answer=${ans[*]}"
set -- $UNITS
if [ "${ans[0]}" = 0 ]; then
    echo -e "${g}==>${o}${b} Service(s) restarted successfully\n${o}"
elif [ -n "${ans[*]}" ]; then
    for n in "${ans[@]}"; do echo -e "${g}==>${o}${b} The ${!n} service has been successfully restarted${o}"; done
    echo -e "\n${g}==>${o}${b} Service(s) restarted successfully\n${o}"
else
    echo -e "${y}==> WARNING:${o}${b} The service(s) restart hasn't been performed\n${o}"
fi
"""

    def go(self, restart, units):
        os.makedirs(engine.STATE_DIR, exist_ok=True)
        write(engine.SETTINGS, json.dumps({"restart_services": restart}), 0o600)
        self.addCleanup(os.remove, engine.SETTINGS)
        self.env(UNITS=" ".join(units))
        self.stub("fake-update", self.UPDATE)
        return self.run_update()

    def test_setting_on(self):
        status, res, log = self.go(True, ["cups.service", "dbus-broker.service", "sshd.service"])
        self.assertIn("answer=1 3", log)
        self.assertEqual(res["services"]["restarted"], ["cups.service", "sshd.service"])
        self.assertEqual(res["services"]["later"], ["dbus-broker.service"])
        self.assertIn("перезапущены сервисы: cups, sshd", res["ok"])
        self.assertIn("dbus-broker перезапустится после перезагрузки — на ходу его не трогаю", res["notes"])
        self.assertFalse(any("сервисы" in w for w in res["warnings"]), res["warnings"])
        with open(engine.summary_path(res["log"]), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["services"]["restarted"], ["cups.service", "sshd.service"])

    def test_setting_on_all_safe(self):
        status, res, log = self.go(True, ["cups.service", "sshd.service"])
        self.assertIn("answer=0", log)
        self.assertEqual(res["services"]["restarted"], ["cups.service", "sshd.service"])

    def test_setting_off(self):
        status, res, log = self.go(False, ["cups.service", "sshd.service"])
        self.assertIn("answer=\n", log)
        self.assertEqual(res["services"]["waiting"], ["cups.service", "sshd.service"])
        self.assertIn("сервисы ждут перезапуска: cups, sshd", res["warnings"])
        self.assertEqual(res["verdict"], 1)

    def test_the_demo_never_restarts(self):
        os.makedirs(engine.STATE_DIR, exist_ok=True)
        write(engine.SETTINGS, json.dumps({"restart_services": True}), 0o600)
        self.addCleanup(os.remove, engine.SETTINGS)
        self.env(UNITS="cups.service")
        self.stub("fake-update", self.UPDATE)
        status, res, log = self.run_update(demo=True)
        self.assertIn("answer=\n", log)


class Probe(Stubs):
    """sudo asks for a password (the Chromebooks): only the update's own terminal session has a ticket, so the
    look as root into the closed /boot is taken there, by the wrapper, right after arch-update. arch-update's
    exit status comes through as it was; nothing of the look shows on the screen or in the journal."""

    def setUp(self):
        super().setUp()
        self.patch(engine, "sudo_asks_password", lambda: True)
        boot = os.path.join(self.tmp, "boot")
        os.makedirs(boot)
        self.kernel_file, self.image = os.path.join(boot, "vmlinuz-linux"), os.path.join(boot, "initramfs-linux.img")
        for f in (self.kernel_file, self.image):
            write(f, "x" * 64, 0o644)
        os.chmod(boot, 0o000)
        self.addCleanup(os.chmod, boot, 0o700)
        if os.access(self.kernel_file, os.F_OK):
            self.skipTest("running as root: every folder is open")
        # what root sees in the folder; the image comes with the update
        write(os.path.join(self.tmp, "root-sees"), f"64 {self.kernel_file}\n", 0o644)
        self.stub("sudo", '#!/bin/bash\ncase "$1" in\n'
                          '-n) shift; [ -f "$T/ticket" ] || exit 1\n    case "$1" in ""|-v|true) exit 0 ;; esac\n'
                          '    exec "$@" ;;\n'
                          '-v) read -rsp "[sudo] password for tester: " pw; echo\n'
                          '    [ "$pw" = secret ] && touch "$T/ticket" || { echo "Sorry, try again."; exit 1; } ;;\n'
                          '*) if [ ! -f "$T/ticket" ]; then read -rsp "[sudo] password for tester: " pw; echo\n'
                          '    [ "$pw" = secret ] && touch "$T/ticket" || exit 1; fi\n    exec "$@" ;;\n'
                          'esac\n')
        # root's view of the closed folder, the image's kernel, and sbctl's verdict
        self.stub("stat", '#!/bin/bash\nf=${!#}\nwhile read -r size path; do\n'
                          '  [ "$path" = "$f" ] && { echo "size $size $f"; exit 0; }\ndone < "$T/root-sees"\nexit 1\n')
        self.stub("lsinitcpio", f'#!/bin/bash\ngrep -qxF "64 $1" "$T/root-sees" || exit 1\n'
                                f'echo "usr/lib/modules/{KVER}/modules.dep"\n')
        self.stub("sbctl", f'#!/bin/bash\n[ "$1" = verify ] && echo "✓ {self.kernel_file} is signed"\n')
        self.patch(engine, "kernel_packages", lambda: {"linux": KVER})
        self.patch(engine, "preset_files", lambda pkg: [("kernel", self.kernel_file), ("image", self.image)])
        self.patch(engine, "secure_boot_on", lambda: True)
        self.patch(engine, "running_kernel", lambda: KVER)
        self.patch(engine, "dkms_built", lambda: {})
        self.patch(tempfile, "tempdir", self.tmp)

    def view(self):
        class View(engine.TerminalView):
            shown = b""

            def out(self, data):
                View.shown += bytes(data)

            def handover(self, why, line):
                engine.RESPONDER.user_input(b"secret\r")
        return View()

    def test_the_closed_boot_is_looked_into_after_the_update(self):
        self.stub("fake-update", '#!/bin/bash\necho "==> Updating Packages..."\n'
                                 f'echo "64 {self.image}" >> "$T/root-sees"\necho "==> The update has been applied"\n')
        view = self.view()
        status, res, log = self.run_update(view)
        self.assertEqual(status, 0)
        self.assertEqual(res["problems"], [])
        self.assertIn("ядро и initramfs на месте", res["ok"])
        self.assertIn("подпись Secure Boot в порядке", res["ok"])
        self.assertEqual(res["warnings"], [])
        self.assertIn("ПРОВЕРКА] после обновления посмотрел с sudo: файлов загрузки 2, initramfs 1, подписи", log)
        for said in ("sudo-ok", "size 64", "is signed", "image 0"):
            self.assertNotIn(said, log)
            self.assertNotIn(said.encode(), type(view).shown)
        self.assertEqual(glob.glob(os.path.join(self.tmp, "arch-update-probe-*")), [])

    def test_the_exit_status_is_kept(self):
        self.stub("fake-update", '#!/bin/bash\necho "==> ERROR: An error has occurred during the update process"\n'
                                 'exit 5\n')
        status, res, _ = self.run_update(self.view())
        self.assertEqual(status, 5)
        self.assertTrue(any("кодом 5" in p for p in res["problems"]))

    def test_killed_by_a_signal_it_stays_killed_by_it(self):
        self.stub("fake-update", '#!/bin/bash\nkill -TERM $$\nsleep 5\n')
        status, _, _ = self.run_update(self.view(), summary=False)
        self.assertEqual(status, 15)

    def test_fixes_run_in_one_session_and_are_looked_at_right_after(self):
        """F: the catalogue's root shell asks for the password once, runs the fixes with their copies and checks,
        and the look as root comes right after it, in the same session."""
        import pexpect
        out, result = os.path.join(self.tmp, "fix.probe"), os.path.join(self.tmp, "fix.result")
        self.stub("mountpoint", '#!/bin/bash\ntest -e "$T/mounted"\n')
        self.stub("mount", f'#!/bin/bash\n: >"$T/mounted"\necho "64 {self.image}" >>"$T/root-sees"\n')
        plan = engine.fix_commands({"boot": [{"kind": "unmounted", "target": "/boot", "text": "t"}]})
        self.assertEqual([f["key"] for f in plan], ["mount"])
        write(result, "", 0o600)
        argv = engine.fixes_command(plan, out, result)
        child = pexpect.spawn(argv[0], argv[1:], env=engine.child_env(), timeout=20)
        child.expect("password for tester")
        child.sendline("secret")
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, fixes.OK)
        self.assertEqual(fixes.read_results(result), [("mount", "ok")])
        probe = engine.read_probe(out)
        self.assertEqual(probe["sizes"], {self.kernel_file: 64, self.image: 64})
        self.assertEqual(probe["images"], {self.image: (0, {KVER})})
        self.assertIn("is signed", probe["sbctl"])
        log = os.path.join(self.tmp, "2026-11-03_10-00-00.log")
        write(log, "", 0o644)
        self.assertEqual(engine.recheck_boot(log, probe=out), [])
        # a fix that fails is put back and said so; the look still comes (the ticket from before still holds)
        os.remove(os.path.join(self.tmp, "mounted"))
        self.stub("mount", "#!/bin/bash\necho 'mount: /boot: special device does not exist.'\nexit 32\n")
        write(result, "", 0o600)
        argv = engine.fixes_command(plan, out, result)
        child = pexpect.spawn(argv[0], argv[1:], env=engine.child_env(), timeout=20)
        child.expect(pexpect.EOF)
        child.close()
        self.assertEqual(child.exitstatus, fixes.ROLLED_BACK)
        self.assertEqual(fixes.read_results(result), [("mount", "rolledback")])
        self.assertIsNotNone(engine.read_probe(out))

    def test_without_a_ticket_the_look_fails_silently(self):
        self.stub("fake-update", '#!/bin/bash\nrm -f "$T/ticket"\necho "==> The update has been applied"\n')
        view = self.view()
        status, res, log = self.run_update(view)
        self.assertEqual(status, 0)
        self.assertNotIn("ПРОВЕРКА]", log)
        self.assertEqual(log.count("ТЕБЕ] запрос пароля"), 1)          # asked once, at the start, never again
        self.assertTrue(any("sudo ls -l /boot" in w for w in res["warnings"]), res["warnings"])
        self.assertTrue(any("sbctl verify" in w for w in res["warnings"]), res["warnings"])


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


class NothingDone(unittest.TestCase):
    """arch-update stops before it touches anything when "Proceed with update?" is answered no (exit 4)
    or another instance holds its lock (exit 17): the summary says the update did not start instead
    of sounding the alarm. Any other failure still counts as a problem."""

    def run_fake(self, script):
        path = os.path.join(tempfile.mkdtemp(), "fake-update")
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + script)
        os.chmod(path, 0o755)
        saved_stdin = os.dup(0)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)                      # no keyboard
        os.close(null)
        self.addCleanup(os.close, saved_stdin)
        self.addCleanup(os.dup2, saved_stdin, 0)
        for name, value in (("unmounted_boot", lambda: []), ("sudo_asks_password", lambda: False)):
            self.addCleanup(setattr, engine, name, getattr(engine, name))
            setattr(engine, name, value)

        class View(engine.TerminalView):
            def out(self, data):
                pass
        status, res, _ = engine.run_update(View(), cmd=path, summary=True)
        engine.LOG.txt.close()
        engine.LOG.raw.close()
        return status, res

    def test_another_instance_is_running(self):
        # what arch-update 4.4 prints (full_upgrade.sh); the engine has to answer "n"
        status, res = self.run_fake("echo \"==> WARNING: There's already a running instance of Arch-Update\"\n"
                                    "read -rp '-> Proceed anyway? [y/N] ' a\n"
                                    "[ \"$a\" = n ] || exit 99\n"
                                    "exit 17\n")
        self.assertEqual(status, 17)
        self.assertFalse(res["started"])
        self.assertEqual(res["problems"], [])
        self.assertIn("другое обновление", res["warnings"][0])
        self.assertIn("не перезагружайся", res["warnings"][0])     # it may be our own run, in pacman

    def test_the_update_was_declined(self):
        status, res = self.run_fake("exit 4\n")
        self.assertEqual(status, 4)
        self.assertFalse(res["started"])
        self.assertEqual(res["problems"], [])
        self.assertIn("отменено", res["warnings"][0])

    def test_a_failed_update_is_still_a_problem(self):
        status, res = self.run_fake("exit 5\n")          # pacman failed
        self.assertEqual(status, 5)
        self.assertTrue(res.get("started", True))
        self.assertTrue(any("кодом 5" in p for p in res["problems"]), res["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
