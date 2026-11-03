#!/usr/bin/env python3
"""Tests of the update reports (lib/report.py): nothing is collected or sent without the user's
consent or in the demo, everything personal is taken out of a report, and a report that cannot be
sent waits for the next try. A local HTTP server stands in for the relay; HOME is a temporary
folder, so the real settings and outbox are never touched.

  python3 tests/test_report.py
"""
import datetime
import errno
import fcntl
import http.server
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

HOME = tempfile.mkdtemp(prefix="arch-update-report-test-")
os.environ["HOME"] = HOME                       # before engine computes its state folder
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import report  # noqa: E402

PERSONAL_LOG = """[17:42:38 СТАРТ] arch-update, режим: автоответы
[sudo] password for cactus:
  -> Downloading brave-bin-1.98.63-x86_64.zip...
Error while writing index for `/home/cactus/.cache/yay/yay-bin/pkg/yay-bin/usr/bin/yay': No debugging symbols
cactus@cactus-chromebook ~ $ ssh-agent on cactus-chromebook.local
wlan0: 3c:22:fb:01:02:03 inet 192.168.1.20/24 fe80::1c2d:3e4f:5a6b:7c8d via 10.0.0.1:53
:: Import PGP key 3B94A80E50A477C7, "Ivan Petrov <ivan.petrov@example.com>"? [Y/n]
machine 0123456789abcdef0123456789abcdef, user Ivan
[17:45:45 ПАКЕТЫ] обновлены: brave-bin 1:1.97.57-1→1:1.98.63-1, gnupg 2.4.9-3→2.4.9-4, archlinux-keyring 20261001-1
[2026-10-30T17:42:47-0700] [ALPM] upgraded linux (7.2.11.arch1-1 -> 7.2.12.arch1-1)
упавшие сервисы: getty@tty1.service user@1000.service
"""
# What the review found getting past the scrubber: unit names, a name before a ".", web addresses
# with a password or a token, a password typed ahead, an address under the user's name, IPv4 after ":".
MORE_LOG = r"""[12:10:00 СЕРВИСЫ] не работали ещё до обновления: syncthing@cactus.service, home-cactus.mount
[12:10:00 ВНИМАНИЕ] упавшие сервисы: run-media-cactus-BACKUP.mount, netctl@wlp2s0\x2dHomeWiFi.service
Hostname set to cactus-chromebook.
built by cactus. Log: /tmp/cactus.log
==> ERROR: Failure while downloading https://cactus:Sup3rS3cret@10.0.0.5/pkgs/foo.tar.gz
==> ERROR: Failure while downloading https://dl.example.com/foo.tar.gz?token=ghp_abcdef123456&sig=zzz9
:: Starting full system upgrade...
hunter2
[sudo] password for cactus:
Contact: Ivan.Petrov@gmail.com cactus@cactus.dev
sshd: Server listening on ::ffff:192.168.0.17 port 22. inet addr:10.0.0.7
"""
WHO = ({"cactus"}, {"cactus-chromebook"}, {"Ivan", "Petrov"}, {"/home/cactus"}, "0123456789abcdef0123456789abcdef")
GONE = (r"(?i)(?<![\w-])(?:cactus|cactus-chromebook|ivan|petrov)(?![\w-])|HomeWiFi|Sup3rS3cret|ghp_|zzz9|hunter2"
        r"|192\.168|10\.0\.0")

# Journals as engine.RunLog has written them from its first version on: what arch-update, pacman and yay print, the
# updater's own lines among it ("[17:45:45 ПАКЕТЫ] …"), and at the end the update's part of pacman.log. Together
# the week of the owner's Chromebook that stopped booting: the update that broke it is the newest.
BROKEN_BOOT = """[17:42:38 СТАРТ] arch-update, режим: автоответы, новости: останавливаться, вид: UIView
[17:42:38 SUDO] sudo спрашивает пароль: спрошу один раз в начале
[sudo] password for cactus:
==> Looking for updates...

==> Packages:
gnupg  2.4.9-3  ->  2.4.9-4
linux  7.2.11.arch1-1  ->  7.2.12.arch1-1

==> AUR Packages:
brave-bin  1:1.97.57-1  ->  1:1.98.63-1
yay  12.5.2-1  ->  12.5.3-1

==> Proceed with update? [Y/n]
[17:42:41 АВТО] «==> Proceed with update? [Y/n]» → y   (proceed with update)
y
:: Proceed with installation? [Y/n]
[17:42:52 АВТО] «:: Proceed with installation? [Y/n]» → y   (pacman proceed)
y
(1/2) upgrading gnupg                            [######################] 100%
(2/2) upgrading linux                            [######################] 100%
:: Running post-transaction hooks...
(3/9) Updating linux initcpios...
==> Building image from preset: /etc/mkinitcpio.d/linux.preset: 'default'
==> ERROR: Initcpio image generation FAILED: '/boot/initramfs-linux.img'
error: command failed to execute correctly
  -> Downloading brave-bin-1.98.63-x86_64.zip...
Error while writing index for `/home/cactus/.cache/yay/yay-bin/pkg/yay-bin/usr/bin/yay': No debugging symbols
==> The update has been applied
[17:45:44 КОНЕЦ] arch-update завершился, код 0
[17:45:45 ПАКЕТЫ] обновлены: brave-bin 1:1.97.57-1→1:1.98.63-1, gnupg 2.4.9-3→2.4.9-4, \
linux 7.2.11.arch1-1→7.2.12.arch1-1, yay 12.5.2-1→12.5.3-1
[17:45:45 ПРОБЛЕМА] в выводе: «Initcpio image generation FAILED»
[17:45:45 ПРОБЛЕМА] в выводе: «error: command failed to execute correctly»
[17:45:45 ПРОБЛЕМА] /boot/initramfs-linux.img пустой — НЕ перезагружайся (сначала: sudo mkinitcpio -P)
[17:45:46 ВНИМАНИЕ] перезагрузись, когда будет удобно (обновились: linux)

══════ /var/log/pacman.log за это обновление ══════
[2026-10-31T17:42:47-0700] [PACMAN] Running 'pacman -Syu'
[2026-10-31T17:43:02-0700] [ALPM] upgraded gnupg (2.4.9-3 -> 2.4.9-4)
[2026-10-31T17:43:10-0700] [ALPM] upgraded linux (7.2.11.arch1-1 -> 7.2.12.arch1-1)
[2026-10-31T17:43:30-0700] [ALPM-SCRIPTLET] ==> ERROR: Initcpio image generation FAILED: '/boot/initramfs-linux.img'
[17:43:31 ПРОБЛЕМА] what a hook printed: not one of the updater's lines
[2026-10-31T17:43:31-0700] [ALPM] transaction completed
"""
# Past midnight, by hand: the end is on the next day.
MIDNIGHT = """[23:58:10 СТАРТ] arch-update, режим: ручной, новости: останавливаться, вид: UIView
==> Proceed with update? [Y/n] y
:: Proceed with installation? [Y/n] y
(1/1) upgrading firefox                          [######################] 100%
[00:03:11 КОНЕЦ] arch-update завершился, код 0
[00:03:12 ПАКЕТЫ] обновлены: firefox 140.0-1→141.0-1
[00:03:12 ПАКЕТЫ] новые: libfoo; удалены: oldlib, oldlib2

══════ /var/log/pacman.log за это обновление ══════
[2026-10-31T00:01:02+0300] [ALPM] upgraded firefox (140.0-1 -> 141.0-1)
"""
# The main PC's usual end: nothing wrong, something to read.
WARNED = """[21:00:00 СТАРТ] arch-update, режим: автоответы, новости: не останавливаться, вид: UIView
:: Proceed with installation? [Y/n]
[21:00:20 АВТО] «:: Proceed with installation? [Y/n]» → y   (pacman proceed)
y
==> Select the service(s) to restart (e.g. 1 3 5), select 0 to restart them all or press "enter" to continue \
without restarting the service(s):
[21:04:10 АВТО] «==> Select the service(s) to restart (e.g. 1 3 5), select 0 to restart them all or press "enter" \
to continue without restarting the service(s):» → Enter   (restart services)
[21:04:30 КОНЕЦ] arch-update завершился, код 0
[21:04:31 ПАКЕТЫ] обновлены: plasma-workspace 6.5.1-1→6.5.2-1
[21:04:31 ПАКЕТЫ] новые: python-foo; удалены: kwayland5, plasma-wayland-protocols
[21:04:32 СЕРВИСЫ] не работали ещё до обновления: bluetooth.service
[21:04:32 ВНИМАНИЕ] есть пакеты-сироты — не удалял, посмотреть: pacman -Qdtq
[21:04:32 ВНИМАНИЕ] arch-update предлагал перезапустить сервисы — пропущено, перезагрузка это решит
"""
DECLINED = """[08:00:00 СТАРТ] arch-update, режим: ручной, новости: останавливаться, вид: UIView
==> Proceed with update? [Y/n] n
[08:00:09 КОНЕЦ] arch-update завершился, код 4
[08:00:10 НЕ НАЧАТО] обновление отменено — система не менялась
"""
UNMOUNTED = """[07:00:00 СТАРТ] arch-update, режим: автоответы, новости: останавливаться, вид: UIView
[07:00:01 НЕ НАЧАТО] /boot не смонтирован — обновление не начинаю (ядро записалось бы не туда)
"""
CUT_OFF = """[06:00:00 СТАРТ] arch-update, режим: автоответы, новости: останавливаться, вид: TerminalView
:: Retrieving packages...
 linux-7.2.12.arch1-1-x86_64 downloading...
"""
CLEAN = """[09:10:02 СТАРТ] arch-update, режим: автоответы, новости: останавливаться, вид: UIView
(1/2) upgrading mesa                             [######################] 100%
(2/2) upgrading vulkan-radeon                    [######################] 100%
[09:12:40 КОНЕЦ] arch-update завершился, код 0
[09:12:41 ПАКЕТЫ] обновлены: mesa 1:26.2.5-1→1:26.2.6-1, vulkan-radeon 1:26.2.5-1→1:26.2.6-1
"""
JOURNALS = [("2026-10-28_09-10-02", CLEAN), ("2026-10-29_06-00-00", CUT_OFF), ("2026-10-29_07-00-00", UNMOUNTED),
            ("2026-10-30_08-00-00", DECLINED), ("2026-10-30_21-00-00", WARNED), ("2026-10-30_23-58-10", MIDNIGHT),
            ("2026-10-31_17-42-38", BROKEN_BOOT)]


def relay_takes(r):
    """What relay/worker.js (validate) asks of a report: a report the tests send is one the real relay would file."""
    u = r.get("update")
    try:
        when = datetime.datetime.strptime(r.get("time"), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return False
    lists = ("changed", "added", "removed", "problems", "warnings", "ok", "notes")
    return (r.get("schema") == 1 and re.fullmatch(r"[0-9a-f]{8}", str(r.get("id"))) is not None
            and 2020 <= when.year <= 2099 and r["time"].endswith("Z") and isinstance(r.get("device"), dict)
            and isinstance(r.get("updater", {}), dict) and isinstance(u, dict)
            and u.get("verdict") in (0, 1, 2, None) and not isinstance(u.get("verdict"), bool)
            and isinstance(u.get("started", True), bool) and (u.get("exit") is None or type(u["exit"]) is int)
            and (u.get("seconds") is None or 0 <= u["seconds"] <= 30 * 86400)
            and all(isinstance(u.get(k, []), list) and all(isinstance(s, str) for s in u.get(k, [])) for k in lists)
            and isinstance(r.get("log"), str))


class Relay(http.server.BaseHTTPRequestHandler):
    """The relay's stand-in: answers every POST with Relay.code (a number, or a function of the
    report that came) and keeps what it got. Like the relay, it answers a report it files with the
    issue's number, and the same report again (the same machine and time: the same log file, see
    putLog in relay/worker.js) with that number, as a duplicate."""
    code, got, filed, headers, lose = 201, [], {}, {}, 0

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Relay.got.append(body)
        code = Relay.code(body) if callable(Relay.code) else Relay.code
        answer = {"ok": 200 <= code < 300}
        key = (body.get("id"), body.get("time")) if isinstance(body, dict) and body.get("log") else None
        if code == 201 and key in Relay.filed:
            code, answer = 200, {"ok": True, "issue": Relay.filed[key], "duplicate": True}
        elif code == 201:
            answer["issue"] = len(Relay.filed) + 1
            Relay.filed[key or len(Relay.got)] = answer["issue"]
        elif code >= 400:
            answer["error"] = "не принят"
        if Relay.lose > 0:                     # filed, but the answer never reaches the machine
            Relay.lose -= 1
            self.close_connection = True
            return
        self.send_response(code)
        for name, value in Relay.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps(answer).encode())

    def log_message(self, *args):
        pass


class Reports(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(HOME, ".local"), ignore_errors=True)
        self.saved_who, self.saved_sender = report._who, report.start_sender
        self.saved_stat, report._UNSAVED = report.engine.PROC_STAT, None
        report.start_sender = lambda: None            # the tests send by hand, in this process
        os.environ["ARCH_UPDATE_REPORT_URL"] = ""     # never the real relay: a test that sends sets its own
        self.server = None
        # This boot began an hour ago: the journals made up here are from earlier boots, unless a test says so.
        self.boot_began = int(time.time()) - 3600
        report.engine.PROC_STAT = os.path.join(HOME, "stat")
        with open(report.engine.PROC_STAT, "w") as f:
            f.write(f"cpu  1 2 3\nbtime {self.boot_began}\n")

    def tearDown(self):
        report._who, report.start_sender = self.saved_who, self.saved_sender
        report.engine.PROC_STAT = self.saved_stat
        os.environ.pop("ARCH_UPDATE_REPORT_URL", None)
        self.stop_relay()

    def relay(self, code=201, headers=None):
        self.stop_relay()
        Relay.code, Relay.got, Relay.filed, Relay.headers, Relay.lose = code, [], {}, headers or {}, 0
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["ARCH_UPDATE_REPORT_URL"] = f"http://127.0.0.1:{self.server.server_port}/report"

    def stop_relay(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def result(self, log_text=PERSONAL_LOG):
        path = os.path.join(HOME, "update.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(log_text)
        return {"verdict": 1, "changed": ["brave-bin", "gnupg"], "added": [], "removed": [], "exitstatus": 0,
                "problems": [], "warnings": ["есть пакеты-сироты"], "ok": ["pacman завершился штатно"], "log": path}

    def waiting(self):
        return sorted(os.listdir(report.OUTBOX)) if os.path.isdir(report.OUTBOX) else []

    def scrub(self, text, who=WHO):
        report._who = lambda: who
        return report.scrubber()(text)

    def tries(self):
        return report._read_json(report.STATUS).get("tries")

    def queue_in_order(self, reports, start=0):
        """Queued oldest first: names of the same second would come in any order."""
        paths = []
        for n, r in enumerate(reports, start):
            path = os.path.join(report.OUTBOX, f"20261031-120000-{n:04x}.json")
            os.replace(report.queue(r), path)
            paths.append(path)
        return paths

    def journal(self, stamp, text, summary=None, folder=None):
        """An update's journal as engine.RunLog leaves it in logs/ (or in folder), with its .raw and, if given, the
        engine's summary next to it (a dict, or the text of a damaged one)."""
        folder = folder or report.LOGS
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, stamp + ".log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        with open(path[:-4] + ".raw", "wb") as f:
            f.write(text.encode())
        if summary is not None:
            with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
                f.write(summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False))
        return path

    def journals(self):
        """The journals of JOURNALS, oldest first, without summaries: from before there were any."""
        return [self.journal(stamp, text) for stamp, text in JOURNALS]

    def zone(self, tz):
        """The machine's time zone for this test (POSIX: "MSK-3" is three hours ahead of UTC)."""
        saved = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.addCleanup(restore)

    # ── consent

    def test_nothing_is_collected_before_the_user_chooses(self):
        self.assertFalse(report.decided())
        self.assertIsNone(report.after_update(self.result()))
        self.assertEqual(self.waiting(), [])
        report.set_consent(False)
        self.assertTrue(report.decided())
        self.assertIsNone(report.after_update(self.result()))
        self.assertEqual(self.waiting(), [])

    def test_the_demo_never_reports(self):
        report.set_consent(True)
        self.assertIsNone(report.after_update(self.result(), demo=True))
        self.assertEqual(self.waiting(), [])

    def test_consent_gives_a_random_id_and_withdrawing_it_drops_the_queue(self):
        s = report.set_consent(True)
        self.assertRegex(s["report_id"], r"^[0-9a-f]{8}$")
        self.assertIsNotNone(report.after_update(self.result()))
        self.assertEqual(len(self.waiting()), 1)
        report.set_consent(False)
        self.assertEqual(self.waiting(), [])
        self.assertEqual(report.set_consent(True)["report_id"], s["report_id"])    # the same machine

    def test_switching_off_holds_when_the_choice_cannot_be_written(self):
        report.set_consent(True)
        report.after_update(self.result())
        real = report._write_json

        def disk_full(path, data):
            raise OSError(errno.ENOSPC, "No space left on device")
        report._write_json = disk_full
        try:
            with self.assertRaises(OSError):              # the settings screen says it could not save
                report.set_consent(False)
        finally:
            report._write_json = real
        self.assertEqual(self.waiting(), [])              # dropped before the write that failed
        self.assertFalse(os.path.exists(report.SETTINGS))   # and without settings.json reports are off
        self.assertFalse(report.enabled())
        self.assertIsNone(report.after_update(self.result()))

    def test_a_report_id_written_by_hand_is_replaced(self):
        report.set_consent(True)
        s = report.settings()
        s["report_id"] = 12345678
        report._write_json(report.SETTINGS, s)
        self.assertEqual(report.report_id(), "")
        self.assertIsNotNone(report.after_update(self.result()))
        self.assertRegex(report.report_id(), r"^[0-9a-f]{8}$")

    # ── what a report holds

    def test_everything_personal_is_taken_out_of_the_log(self):
        text = self.scrub(PERSONAL_LOG)
        for gone in ("cactus", "Ivan", "Petrov", "3c:22:fb", "192.168.1.20", "10.0.0.1", "fe80::",
                     "ivan.petrov@example.com", "0123456789abcdef0123456789abcdef"):
            self.assertNotIn(gone, text)
        for kept in ("password for <user>:", "~/.cache/yay/yay-bin", "brave-bin 1:1.97.57-1→1:1.98.63-1",
                     "gnupg 2.4.9-3→2.4.9-4", "archlinux-keyring 20261001-1", "7.2.11.arch1-1 -> 7.2.12.arch1-1",
                     "[17:42:38 СТАРТ]", "[2026-10-30T17:42:47-0700]", "getty@tty1.service user@1000.service",
                     "3B94A80E50A477C7", "<user>@<host> ~ $ ssh-agent on <host>.local"):
            self.assertIn(kept, text)

    def test_names_that_are_common_words_leave_the_log_readable(self):
        report._who = lambda: ({"arch"}, {"archlinux"}, set(), {"/home/arch"}, "")
        text = report.scrubber()("Arch Linux: arch-update, archlinux-keyring; [sudo] password for arch:\n"
                                 "/home/arch/.cache/yay/x and archlinux.org; arch-update.timer, home-arch.mount\n")
        self.assertIn("Arch Linux: arch-update, archlinux-keyring", text)
        self.assertIn("password for <user>:", text)
        self.assertIn("~/.cache/yay/x and archlinux.org; arch-update.timer, home-<user>.mount", text)

    def test_unit_names_carry_no_user_or_wifi_name(self):
        units = (r"syncthing@cactus.service, home-cactus.mount, run-media-cactus-BACKUP.mount, "
                 r"mnt-cactus\x2dfiles.mount, home-cactus-Music.automount, netctl@wlp2s0\x2dHomeWiFi.service, "
                 r"wpa_supplicant@wlp2s0.service, getty@tty1.service, serial-getty@ttyS0.service, "
                 r"user@1000.service, user-runtime-dir@1000.service, arch-update-cactus-sync.service, "
                 r"ping@192.168.1.5.service, sshd@3-192.168.1.20:22-192.168.1.5:51234.service")
        text = self.scrub(f"упавшие сервисы: {units}\n[12:10:00 СЕРВИСЫ] не работали ещё до обновления: {units}\n")
        self.assertNotRegex(text, r"(?i)cactus(?!-sync\.service)|HomeWiFi|192\.168")
        for kept in ("syncthing@<user>.service", "home-<user>.mount", "run-media-<user>-BACKUP.mount",
                     r"mnt-<user>\x2dfiles.mount", "home-<user>-Music.automount", "netctl@<instance>.service",
                     "wpa_supplicant@<instance>.service", "getty@tty1.service", "serial-getty@ttyS0.service",
                     "user@1000.service", "user-runtime-dir@1000.service", "arch-update-cactus-sync.service",
                     "ping@<instance>.service", "sshd@<instance>.service"):
            self.assertEqual(text.count(kept), 2, kept)
        # a user name with a "-" is written \x2d in a path unit
        self.assertEqual(self.scrub(r"home-ivan\x2dp.mount syncthing@ivan-p.service",
                                    ({"ivan-p"}, set(), set(), {"/home/ivan-p"}, "")),
                         "home-<user>.mount syncthing@<user>.service")

    def test_failed_services_reach_the_report_without_names(self):
        report._who = lambda: WHO
        report.set_consent(True)
        failed = r"syncthing@cactus.service, netctl@wlp2s0\x2dHomeWiFi.service"
        res = self.result(f"[12:10:00 ВНИМАНИЕ] упавшие сервисы: {failed}\n")
        res["warnings"] = [f"упавшие сервисы: {failed}"]
        r = report.build(res)
        clean = "упавшие сервисы: syncthing@<user>.service, netctl@<instance>.service"
        self.assertEqual(r["update"]["warnings"], [clean])
        self.assertIn(clean, r["log"])

    def test_a_name_ends_at_a_dot_too(self):
        text = self.scrub("ssh on cactus-chromebook.local\nHostname set to cactus-chromebook.\n"
                          "built by cactus. Log: /tmp/cactus.log, signed: Ivan.\nArch Update: arch-update-cactus\n")
        self.assertNotRegex(text, r"(?i)(?<![\w-])(?:cactus|cactus-chromebook|ivan)(?![\w-])")
        for kept in ("on <host>.local", "set to <host>.", "by <user>.", "/tmp/<user>.log", "signed: <name>.",
                     "arch-update-cactus"):
            self.assertIn(kept, text)
        self.assertEqual(self.scrub("Иван Петров. ИВАН петров", ({"ivan"}, set(), {"Иван", "Петров"}, set(), "")),
                         "<name> <name>. <name> <name>")

    def test_numbers_after_the_real_name_are_no_names(self):
        fake = pwd.struct_passwd(("cactus", "x", 1000, 1000, "Ivan Petrov,101,555-1234,", "/home/cactus", "/bin/bash"))
        real = pwd.getpwuid
        pwd.getpwuid = lambda uid: fake
        try:
            names = report._who()[2]
        finally:
            pwd.getpwuid = real
        self.assertEqual(names, {"Ivan", "Petrov"})       # 1.101.0 stays a version

    def test_passwords_and_tokens_in_web_addresses_are_taken_out(self):
        text = self.scrub(
            "==> ERROR: Failure while downloading https://cactus:Sup3rS3cret@10.0.0.5/pkgs/foo.tar.gz\n"
            "==> ERROR: Failure while downloading https://bob:hunter2@example.com/x.tar.gz\n"
            "fatal: unable to access 'https://ghp_tok3n111@github.com/owner/repo.git/'\n"
            "https://dl.example.com/foo.tar.gz?token=ghp_abcdef123456&sig=zzz9&raw=true\n"
            "https://x.example.org/cb#access_token=acc111&expires_in=3600\n"
            "https://s3.example.com/b/k?X-Amz-Credential=AKIAEXAMPLE%2F20261031&X-Amz-Signature=deadbeef01\n"
            "https://api.example.com/v1?api_key=kk111&apikey=kk222&password=pp111&passwd=pp222&pwd=pp333"
            "&secret=ss111&auth=aa111&key=kk333&signature=gg111&private_token=tt111&client_secret=ss222\n"
            "https://aur.archlinux.org/yay.git https://example.com/file.tar.gz?raw=true&ref=v1.2\n")
        for gone in ("Sup3rS3cret", "bob", "hunter2", "ghp_", "zzz9", "acc111", "AKIAEXAMPLE", "deadbeef01", "kk111",
                     "kk222", "pp111", "pp222", "pp333", "ss111", "aa111", "kk333", "gg111", "tt111", "ss222"):
            self.assertNotIn(gone, text)
        for kept in ("https://<credentials>@<ip>/pkgs/foo.tar.gz", "https://<credentials>@example.com/x.tar.gz",
                     "'https://<credentials>@github.com/owner/repo.git/'", "?token=<secret>&sig=<secret>&raw=true",
                     "#access_token=<secret>&expires_in=3600", "&X-Amz-Signature=<secret>\n",
                     "&client_secret=<secret>\n",
                     "https://aur.archlinux.org/yay.git https://example.com/file.tar.gz?raw=true&ref=v1.2\n"):
            self.assertIn(kept, text)
        self.assertNotIn("<email>", text)                 # bob:hunter2@example.com is no address

    def test_a_password_typed_ahead_is_taken_out(self):
        text = self.scrub(":: Starting full system upgrade...\nhunter2\n[sudo] password for cactus: \ngot 7 chars\n"
                          "Пароль1\n[sudo] пароль для cactus: \n"
                          "s3cret!\nEnter passphrase for key '/home/cactus/.ssh/id_ed25519': \n"
                          "hunter3\nPassword:")
        for gone in ("hunter2", "Пароль1", "s3cret!", "hunter3"):
            self.assertNotIn(gone, text)
        self.assertEqual(text.count("<ввод до запроса пароля>\n"), 4)
        self.assertTrue(text.startswith(":: Starting full system upgrade...\n<ввод до запроса пароля>\n"
                                        "[sudo] password for <user>: \ngot 7 chars\n"))
        kept = "==> Retrieving sources...\nfoo.tar.gz\n  -> Found foo.tar.gz\nok\nnew password set for the key\n"
        self.assertEqual(self.scrub(kept), kept)          # no prompt below: no answer typed ahead

    def test_a_password_typed_ahead_is_not_left_at_the_cut(self):
        # the head of a long log would end with the typed line and the prompt below it would be cut away
        head = report.LOG_LIMIT // 4
        log = ("x" * 99 + "\n") * ((head - 20) // 100)
        log += "y" * (head - 20 - len(log) - 1) + "\n"
        log += "hunter2\n[sudo] password for cactus: \n" + ("z" * 99 + "\n") * (report.LOG_LIMIT // 100)
        text = report._log_text(self.result(log)["log"])
        self.assertIn("\n…[пропущено ", text)
        self.assertNotIn("hunter2", text)

    def test_an_address_under_the_users_name_is_an_address(self):
        who = ({"zorkowski"}, {"zork-laptop"}, {"Zbigniew", "Zorkowski"}, {"/home/zorkowski"}, "")
        text = self.scrub("Contact: Zbigniew.Zorkowski@gmail.com zorkowski@zorkowski.dev ZORKOWSKI@EXAMPLE.ORG\n"
                          'Signed by "Zbigniew Zorkowski <zbigniew@zorkowski.dev>"\n', who)
        self.assertTrue(text.startswith("Contact: <email> <email> <email>\n"))
        self.assertIn("<<email>>", text)
        self.assertNotRegex(text, r"(?i)zork|zbigniew")

    def test_an_ipv4_address_after_a_colon_is_taken_out(self):
        text = self.scrub("sshd: Server listening on ::ffff:192.168.0.17 port 22.\n"
                          "inet addr:10.0.0.7  Bcast:10.0.0.255\n"
                          "upgraded foo (1:1.2.3-1 -> 1:1.2.4-1), brave-bin 1:1.98.63-1\n")
        self.assertIn("::ffff:<ip> port 22.", text)
        self.assertIn("inet addr:<ip>  Bcast:<ip>", text)
        self.assertIn("(1:1.2.3-1 -> 1:1.2.4-1), brave-bin 1:1.98.63-1", text)

    def test_scrubbing_twice_changes_nothing(self):
        report._who = lambda: WHO
        scrub = report.scrubber()
        for text in (PERSONAL_LOG, MORE_LOG):
            once = scrub(text)
            self.assertEqual(scrub(once), once)
            self.assertNotRegex(once, GONE)
        # names that are the scrubber's own words: Mac is taken out, <mac> is left alone
        report._who = lambda: ({"name"}, {"host"}, {"Mac", "Secret"}, {"/home/name"}, "")
        scrub = report.scrubber()
        once = scrub(PERSONAL_LOG + "Mac Secret on host.local, name\nhttps://x/?key=abc\n")
        self.assertEqual(scrub(once), once)
        self.assertIn("<name> <name> on <host>.local, <user>\n", once)
        self.assertIn("<mac> inet", once)

    def test_the_scrubber_is_quick_whatever_it_reads(self):
        report._who = lambda: WHO
        scrub = report.scrubber()
        n = report.LOG_LIMIT
        texts = {"a long update log": ((PERSONAL_LOG + MORE_LOG) * (n // 1000))[:n], "a.a.a": "a." * (n // 2),
                 "@a.a.a": "@" + "a." * (n // 2), "-cactus": "-cactus" * (n // 7),
                 r"\x2dcactus": r"\x2dcactus" * (n // 10), "&token": "&token" * (n // 6),
                 "&key-key": "&" + "key-" * (n // 4), "?a=b&c=d": "?a=b&c=d" * (n // 8), "://a:": "://a:" + "b" * n,
                 "a@a.a.a": "a@" + "a." * (n // 2), "typed ahead": "x\npassword:\n" * (n // 12),
                 "password run": "x\n" + "password" * (n // 8), "1:1:1": "1:" * (n // 2), "a::a::": "a::" * (n // 3),
                 "@1111": "@" + "1" * n}
        for name, text in texts.items():
            t0 = time.perf_counter()
            scrub(text)
            # each rule reads the text once: a tenth of a second here; one that goes back over it takes minutes
            self.assertLess(time.perf_counter() - t0, 5, name)

    def test_a_report_of_this_machine_names_nobody(self):
        # the real names of the machine the test runs on, wherever they turn up
        users, hosts, names, homes, machine = report._who()
        log = PERSONAL_LOG + "".join(f"\n{u} {u}@{h} {home}/x {h}. {u}."
                                     for u in users for h in hosts for home in homes)
        report.set_consent(True)
        text = json.dumps(report.build(self.result(log), mode="auto", seconds=187,
                                       ui={"screen": "80x24", "monitors": 1}), ensure_ascii=False)
        for word in users | hosts | names:
            if len(word) >= 3 and word.lower() not in report.COMMON:
                self.assertNotRegex(text, r"(?i)(?<![\w-])" + re.escape(word) + r"(?![\w-])")
        for home in homes:
            self.assertNotIn(home + "/", text)
        if machine:
            self.assertNotIn(machine, text)

    def test_a_report_holds_the_update_and_the_machine(self):
        report.set_consent(True)
        r = report.build(self.result(), mode="auto", seconds=187.4, ui={"screen": "80x24", "monitors": 1})
        self.assertEqual(r["schema"], 1)
        self.assertEqual(r["id"], report.report_id())
        self.assertEqual(r["update"]["verdict"], 1)
        self.assertEqual(r["update"]["changed"], ["brave-bin", "gnupg"])
        self.assertEqual(r["update"]["seconds"], 187)
        self.assertEqual(r["device"]["screen"], "80x24")
        self.assertIn("network", r["device"])
        self.assertNotIn("ssid", json.dumps(r).lower())
        self.assertIn("gnupg 2.4.9-3→2.4.9-4", r["log"])

    def test_a_long_log_keeps_its_head_and_tail(self):
        report.set_consent(True)
        log = "начало\n" + "x" * (report.LOG_LIMIT * 2) + "\nконец\n"
        r = report.build(self.result(log))
        self.assertLess(len(r["log"]), report.LOG_LIMIT + 200)
        self.assertTrue(r["log"].startswith("начало"))
        self.assertTrue(r["log"].endswith("конец\n"))

    def test_a_long_log_is_cut_between_lines(self):
        # A long yay build, padded so that both cuts would fall inside the user's name: "/home/zork|owski".
        report._who = lambda: ({"zorkowski"}, {"zork-laptop"}, set(), {"/home/zorkowski"}, "")
        report.set_consent(True)
        line = "  CC [M]  /home/zorkowski/.cache/yay/linux-zen/src/drivers/gpu/drm/i915/intel_{:05d}.o\n"
        width, at = len(line.format(0)), line.index("zorkowski") + 4
        body = "".join(line.format(i) for i in range(report.LOG_LIMIT * 2 // width))
        head_pad = (report.LOG_LIMIT // 4 - 1 - at) % width
        tail_pad = (at - len(body) - 1 + report.LOG_LIMIT * 3 // 4) % width
        log = "x" * head_pad + "\n" + body + "y" * tail_pad + "\n"
        self.assertEqual(log[report.LOG_LIMIT // 4 - 4:report.LOG_LIMIT // 4], "zork")    # the old cut, in the name
        self.assertEqual(log[len(log) - report.LOG_LIMIT * 3 // 4:][:5], "owski")
        r = report.build(self.result(log))
        self.assertNotRegex(r["log"], r"(?i)zork")
        head, _, tail = r["log"].partition("\n…[пропущено ")
        self.assertRegex(head, r"intel_\d{5}\.o$")                   # whole lines on both sides of the cut
        self.assertRegex(tail.split("\n", 1)[1], r"^  CC \[M\]  ~/\.cache/")
        self.assertLess(len(r["log"]), report.LOG_LIMIT + 200)

    def test_a_log_of_one_long_line_is_cut_between_words(self):
        head, tail = report._cut("word " * report.LOG_LIMIT)
        self.assertTrue(head.endswith(" word") and tail.startswith("word "))
        self.assertLessEqual(len(head), report.LOG_LIMIT // 4)
        self.assertLessEqual(len(tail), report.LOG_LIMIT * 3 // 4)
        head, tail = report._cut("x" * report.LOG_LIMIT * 2)          # only a word that long is cut where it falls
        self.assertEqual((len(head), len(tail)), (report.LOG_LIMIT // 4, report.LOG_LIMIT * 3 // 4))

    # ── sending

    def test_a_report_is_sent_and_kept(self):
        self.relay()
        report._who = lambda: WHO
        report.set_consent(True)
        path = report.after_update(self.result(), mode="auto", seconds=10)
        self.assertEqual(report.state_of(path), "waiting")
        self.assertEqual(report.send_pending(), (1, 0))
        self.assertEqual(report.state_of(path), "sent")
        got = json.dumps(Relay.got[0], ensure_ascii=False)
        self.assertEqual(Relay.got[0]["id"], report.report_id())
        for gone in ("cactus", "Ivan", "192.168.1.20"):
            self.assertNotIn(gone, got)
        self.assertTrue(report.status()["last_sent"])

    def test_a_report_queued_before_goes_out_scrubbed_again(self):
        # one an older version left in the outbox, with what it did not take out yet
        self.relay()
        report._who = lambda: WHO
        report.set_consent(True)
        old = {"schema": 1, "id": report.report_id(), "time": "2026-10-30T12:00:00Z", "device": {"cpu": "x"},
               "update": {"verdict": 1, "warnings": [r"упавшие сервисы: syncthing@cactus.service, "
                                                     r"netctl@wlp2s0\x2dHomeWiFi.service"]},
               "log": MORE_LOG}
        path = report.queue(old)
        self.assertEqual(report.send_pending(), (1, 0))
        got = Relay.got[0]
        self.assertNotRegex(json.dumps(got, ensure_ascii=False), GONE)
        clean = "упавшие сервисы: syncthing@<user>.service, netctl@<instance>.service"
        self.assertEqual(got["update"]["warnings"], [clean])
        self.assertEqual((got["id"], got["time"]), (old["id"], old["time"]))
        with open(os.path.join(report.SENT, os.path.basename(path)), encoding="utf-8") as f:
            self.assertEqual(json.load(f), got)              # what is kept is what went out
        self.assertNotIn("HomeWiFi", report.preview())

    def test_a_damaged_report_is_set_aside_unsent(self):
        self.relay()
        report.set_consent(True)
        os.makedirs(report.OUTBOX)
        bad = os.path.join(report.OUTBOX, "20261031-120000-dead.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"schema": 1, "log": "half of it')
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(Relay.got, [])
        self.assertEqual(report.state_of(bad), "rejected")

    def test_offline_or_a_busy_relay_keeps_it_for_later(self):
        report.set_consent(True)
        path = report.after_update(self.result())
        os.environ["ARCH_UPDATE_REPORT_URL"] = "http://127.0.0.1:9/report"     # nobody listens on port 9
        self.assertEqual(report.send_pending(timeout=3), (0, 1))
        self.assertEqual(report.state_of(path), "waiting")
        self.assertTrue(report.status()["last_error"])
        self.relay(503)
        self.assertEqual(report.send_pending(), (0, 1))
        self.relay(201)
        self.assertEqual(report.send_pending(), (1, 0))

    def test_a_busy_relay_is_not_asked_again_in_the_same_round(self):
        for code in (408, 429, 503):
            with self.subTest(code=code):
                shutil.rmtree(report.REPORTS, ignore_errors=True)
                self.relay(code)
                report.set_consent(True)
                for n in range(3):
                    report.queue({"schema": 1, "n": n})
                self.assertEqual(report.send_pending(), (0, 3))
                self.assertEqual(len(Relay.got), 1)              # the others would get the same answer now
                self.assertEqual(report.status()["last_error"], f"HTTP {code}")
                self.assertEqual(self.tries(), {})                # and nobody's fault: no try counted

    def test_a_report_the_relay_fails_on_does_not_hold_up_the_others(self):
        for code in (500,):
            with self.subTest(code=code):
                shutil.rmtree(report.REPORTS, ignore_errors=True)
                self.relay(lambda r, code=code: code if r.get("n") == 0 else 201)
                report.set_consent(True)
                stuck, *others = self.queue_in_order([{"schema": 1, "n": n} for n in range(3)])
                name = os.path.basename(stuck)
                self.assertEqual(report.send_pending(), (2, 1))          # the oldest, first, held up nothing
                self.assertEqual([report.state_of(p) for p in others], ["sent", "sent"])
                self.assertEqual(self.tries(), {name: 1})
                self.assertEqual(report.status()["last_error"], "")     # the last one went through
                self.assertEqual(report.send_pending(), (0, 1))
                self.assertEqual((self.tries(), report.status()["last_error"]), ({name: 2}, f"HTTP {code}"))
                self.assertEqual(report.state_of(stuck), "waiting")
                self.assertEqual(report.send_pending(), (0, 0))          # the third time it is set aside
                self.assertEqual(report.state_of(stuck), "rejected")
                self.assertEqual(self.tries(), {})
                self.assertTrue(report.status()["last_error"].startswith(f"HTTP {code}: "))
                self.assertEqual([r["n"] for r in Relay.got].count(0), report.TRIES)

    def test_github_failing_for_every_report_keeps_them_all(self):
        # An expired token: the relay answers 502 to every report alike. Nothing is anyone's fault, nothing
        # is set aside, and two such answers end the round.
        for code in (502, 504):
            with self.subTest(code=code):
                shutil.rmtree(report.REPORTS, ignore_errors=True)
                self.relay(code)
                report.set_consent(True)
                paths = self.queue_in_order([{"schema": 1, "n": n} for n in range(4)])
                for _ in range(5):
                    self.assertEqual(report.send_pending(), (0, 4))
                self.assertEqual([report.state_of(p) for p in paths], ["waiting"] * 4)
                self.assertEqual(self.tries(), {})
                self.assertEqual(len(Relay.got), 5 * 2)
                self.assertEqual(report.status()["last_error"], f"HTTP {code}")

    def test_a_report_github_keeps_refusing_is_set_aside_once_others_go_through(self):
        # 502 on one report only: it counts a try only in a round where another report went through.
        self.relay(lambda r: 502 if r.get("n") == 0 else 201)
        report.set_consent(True)
        stuck, = self.queue_in_order([{"schema": 1, "n": 0}])
        name = os.path.basename(stuck)
        self.assertEqual(report.send_pending(), (0, 1))                 # alone: it cannot tell, nothing counted
        self.assertEqual(self.tries(), {})
        for round_ in range(1, report.TRIES + 1):
            self.queue_in_order([{"schema": 1, "n": round_}], start=round_)
            self.assertEqual(report.send_pending()[0], 1)
            if round_ < report.TRIES:
                self.assertEqual(self.tries(), {name: round_})
                self.assertEqual(report.status()["last_error"], "")     # the last report went through
        self.assertEqual(report.state_of(stuck), "rejected")
        self.assertEqual(self.tries(), {})
        self.assertIn("не принят", report.status()["last_error"])

    def test_a_relay_at_the_wrong_address_keeps_the_reports(self):
        for code in (404, 405):
            with self.subTest(code=code):
                shutil.rmtree(report.REPORTS, ignore_errors=True)
                self.relay(code)
                report.set_consent(True)
                paths = self.queue_in_order([{"schema": 1, "n": n} for n in range(2)])
                self.assertEqual(report.send_pending(), (0, 2))
                self.assertEqual([report.state_of(p) for p in paths], ["waiting", "waiting"])

    def test_a_manual_journal_loses_what_was_typed_ahead_of_a_password_prompt(self):
        report._who = lambda: WHO
        typed = ("[12:00:00 СТАРТ] arch-update, режим: ручной\n"
                 "correct horse battery staple\nhunter2\nhunter2\n"     # typed ahead: with spaces, twice
                 "hunt[sudo] password for cactus: \n"                     # and on the prompt's own line
                 "[12:00:03 SUDO] пароль принят\n"
                 "==> Looking for updates...\n"
                 "[12:00:04 ТЕБЕ] запрос пароля: «Password:»\n"
                 "hunter3 Password: \n"
                 "[12:00:05 ТЫ] ответил: «•••»\n")
        for mode, log in (("manual", typed), ("auto", typed.replace("режим: ручной", "режим: автоответы")
                                               + "[12:00:06 РУЧНОЙ] Ctrl+] — ручной режим до конца обновления\n")):
            with self.subTest(mode=mode):
                report.set_consent(True)
                text = report.build(self.result(log), mode=mode)["log"]
                for gone in ("correct horse", "hunter2", "hunt[", "hunter3"):
                    self.assertNotIn(gone, text)
                for kept in ("[12:00:00 СТАРТ]", "[sudo] password for <user>:", "==> Looking for updates...",
                             "Password:", "[12:00:05 ТЫ] ответил: «•••»"):
                    self.assertIn(kept, text)
        # an automatic update's journal is left as it is: there the keys never reach the program
        text = report.build(self.result("==> Looking for updates...\nok then\n[sudo] password for cactus: \n"))["log"]
        self.assertIn("ok then", text)

    def test_an_erased_typo_is_not_sent_as_typed(self):
        # The pty echoes a Backspace as "\b \b": the journal keeps what was finally typed, not "hunterr 2".
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import engine
        log = engine.RunLog()
        try:
            log.output(b"hunterr\x08 \x082\n")
            log.txt.flush()
            with open(log.path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "hunter2\n")
        finally:
            log.txt.close()
            log.raw.close()

    def test_a_refused_report_is_set_aside_not_retried(self):
        self.relay(400)
        report.set_consent(True)
        path = report.after_update(self.result())
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(report.state_of(path), "rejected")

    def test_without_a_relay_reports_wait(self):
        report.set_consent(True)                     # setUp left no server known
        report.after_update(self.result())
        self.assertFalse(report.status()["endpoint"])
        self.assertEqual(report.send_pending(), (0, 1))

    def test_the_relay_is_the_developers_worker(self):
        self.assertRegex(report.ENDPOINT, r"^https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev/report$")
        os.environ.pop("ARCH_UPDATE_REPORT_URL")
        self.assertEqual(report.endpoint(), report.ENDPOINT)

    def test_nothing_is_sent_once_consent_is_withdrawn(self):
        self.relay()
        report.set_consent(True)
        report.after_update(self.result())
        report.set_consent(False)
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(Relay.got, [])

    def test_switched_off_while_sending_nothing_more_goes(self):
        # switched off (and the outbox somehow still there) while the first report is on its way
        def switch_off(body):
            report._write_json(report.SETTINGS, dict(report.settings(), reports=False))
            return 201
        self.relay(switch_off)
        report.set_consent(True)
        self.queue_in_order([{"schema": 1, "n": n} for n in range(3)])
        self.assertEqual(report.send_pending(), (1, 2))
        self.assertEqual(len(Relay.got), 1)

    def test_a_garbled_answer_keeps_the_report_for_later(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def garble():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall(b"garbage\r\n\r\n")          # no status line: http.client.BadStatusLine
            conn.close()
        threading.Thread(target=garble, daemon=True).start()
        os.environ["ARCH_UPDATE_REPORT_URL"] = f"http://127.0.0.1:{server.getsockname()[1]}/report"
        try:
            report.set_consent(True)
            path = report.after_update(self.result())
            self.assertEqual(report.send_pending(timeout=5), (0, 1))
        finally:
            server.close()
        self.assertEqual(report.state_of(path), "waiting")
        self.assertTrue(report.status()["last_error"].startswith("BadStatusLine"))

    def test_the_outbox_never_grows_without_end(self):
        report.set_consent(True)
        for _ in range(report.KEEP[report.OUTBOX] + 5):
            report.queue({"schema": 1})
        self.assertEqual(len(self.waiting()), report.KEEP[report.OUTBOX])

    def test_a_failed_write_leaves_no_temp_file(self):
        path = os.path.join(report.OUTBOX, "20261031-120000-beef.json")

        def half_then_full(data, f, **kw):
            f.write('{"schema": ')
            raise OSError(errno.ENOSPC, "No space left on device")
        real = report.json
        report.json = types.SimpleNamespace(dump=half_then_full)
        try:
            with self.assertRaises(OSError):
                report._write_json(path, {"schema": 1})
        finally:
            report.json = real
        self.assertEqual(os.listdir(report.OUTBOX), [])           # neither the report nor its temp file
        # one a write killed halfway left behind goes after a day, a fresh one may still be written
        old, new = (os.path.join(report.OUTBOX, f"{n}.json.4242.tmp") for n in ("old", "new"))
        stale = os.path.join(report.REPORTS, "status.json.4242.tmp")
        for p in (old, new, stale):
            open(p, "w").close()
        for p in (old, stale):
            os.utime(p, (time.time() - 2 * 86400,) * 2)
        report.set_consent(True)
        report.queue({"schema": 1})
        self.assertEqual((os.path.exists(old), os.path.exists(new)), (False, True))
        self.relay()
        report.send_pending()
        self.assertFalse(os.path.exists(stale))

    def test_a_damaged_status_file_breaks_nothing(self):
        os.makedirs(report.REPORTS)
        for junk in ('{"last_error": 500, "last_sent": ["2026-10-31"], "tries": "x"}', '{"last_error": null}',
                     '[1, 2]', 'not json', '{"tries": {"a.json": "1", "b.json": true}}'):
            with open(report.STATUS, "w", encoding="utf-8") as f:
                f.write(junk)
            st = report.status()
            self.assertEqual((type(st["last_error"]), type(st["last_sent"]), type(st["id"])), (str, str, str), junk)
        self.relay()
        report.set_consent(True)
        report.queue({"schema": 1})
        self.assertEqual(report.send_pending(), (1, 0))

    # ── the updates kept on this machine, and a report sent by hand

    def test_updates_from_before_the_summaries_are_listed_from_their_journals(self):
        paths = self.journals()
        os.symlink(os.path.basename(paths[-1]), os.path.join(report.LOGS, "latest.log"))
        open(os.path.join(report.LOGS, "notes.txt"), "w").close()
        got = report.local_updates()
        self.assertEqual([u["log"] for u in got], paths[:-6:-1])             # the newest five, newest first
        broken, midnight, warned, declined, unmounted = got
        self.assertEqual((broken["verdict"], broken["started"], broken["mode"], broken["exitstatus"]),
                         (2, True, "auto", 0))
        self.assertEqual((broken["changed"], broken["added"], broken["packages"]),
                         (["brave-bin", "gnupg", "linux", "yay"], [], 4))
        self.assertEqual(broken["problems"], ["в выводе: «Initcpio image generation FAILED»",
                                              "в выводе: «error: command failed to execute correctly»",
                                              "/boot/initramfs-linux.img пустой — НЕ перезагружайся "
                                              "(сначала: sudo mkinitcpio -P)"])     # not what a hook printed
        self.assertEqual(broken["warnings"], ["перезагрузись, когда будет удобно (обновились: linux)"])
        self.assertEqual((broken["time"], broken["ended"], broken["seconds"]),
                         ("2026-10-31T17:42:38", "2026-10-31T17:45:46", 188))
        self.assertEqual((broken["sent"], broken["issue"], broken["notes"], broken["waiting"]), ("", None, [], False))
        self.assertEqual((midnight["verdict"], midnight["mode"], midnight["ended"], midnight["seconds"]),
                         (0, "manual", "2026-10-31T00:03:12", 302))
        self.assertEqual((midnight["changed"], midnight["added"], midnight["removed"], midnight["packages"]),
                         (["firefox"], ["libfoo"], ["oldlib", "oldlib2"], 4))
        self.assertEqual((warned["verdict"], warned["problems"], len(warned["warnings"]), warned["packages"]),
                         (1, [], 2, 4))
        self.assertEqual((declined["verdict"], declined["started"], declined["exitstatus"], declined["warnings"]),
                         (1, False, 4, ["обновление отменено — система не менялась"]))
        self.assertEqual((unmounted["verdict"], unmounted["started"], unmounted["packages"]), (2, False, 0))
        self.assertIn("не смонтирован", unmounted["problems"][0])
        everything = report.local_updates(10)
        self.assertEqual([u["log"] for u in everything], paths[::-1])
        cut_off, clean = everything[-2:]
        self.assertEqual((cut_off["verdict"], cut_off["started"], cut_off["exitstatus"]), (None, True, None))
        self.assertEqual((clean["verdict"], clean["changed"]), (0, ["mesa", "vulkan-radeon"]))
        self.assertEqual(report.local_updates(0), [])

    def test_the_engines_summary_counts_where_there_is_one(self):
        summary = {"verdict": 1, "changed": ["mesa"], "added": [], "removed": [], "problems": [],
                   "warnings": ["есть .pacnew файлы — разберём вместе (pacdiff -o покажет список)"],
                   "notes": ["сервисы не перезапускались"], "ok": ["pacman завершился штатно"], "exitstatus": 0,
                   "started": True, "mode": "manual", "began": "2026-11-02T10:00:00", "ended": "2026-11-02T10:01:30",
                   "boot_id": "6f0a…", "services": {"listed": [], "restarted": [], "skipped": []}, "fixes": []}
        path = self.journal("2026-11-02_10-00-00", BROKEN_BOOT, summary)     # its journal says otherwise
        u, = report.local_updates()
        self.assertEqual((u["verdict"], u["mode"], u["changed"], u["problems"], u["notes"], u["ok"], u["seconds"]),
                         (1, "manual", ["mesa"], [], summary["notes"], summary["ok"], 90))
        self.assertFalse(u["resolved"])
        report._write_json(path[:-4] + ".json", dict(summary, verdict=2, problems=["нет /boot/vmlinuz-linux"],
                                                     resolved=True))
        self.assertTrue(report.local_updates()[0]["resolved"])
        self.assertIn("✓ исправлено", report.listing())
        # One half written, or of another shape, is no summary: the journal is read instead.
        for junk in ('{"verdict": 1, "chan', "[1, 2]", '{"verdict": "yes"}', '{"verdict": true}', "{}"):
            with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
                f.write(junk)
            u, = report.local_updates()
            self.assertEqual((u["verdict"], len(u["problems"]), u["mode"]), (2, 3, "auto"), junk)

    def test_odd_files_in_the_journals_folder_break_nothing(self):
        self.journal("2026-10-01_10-00-00", "")
        with open(os.path.join(report.LOGS, "2026-10-02_10-00-00.log"), "wb") as f:
            f.write(bytes(range(256)) * 64 + b"\n[1\xff:00:00 \xd0\x9f] x\n")
        self.journal("2026-10-03_10-00-00", "[99:99:99 ПРОБЛЕМА] нет\n[25:00:00 КОНЕЦ] код x\n[10:00:01 КОНЕЦ]\n"
                                            "[10:00:02 ПАКЕТЫ] новые: ; удалены\n")
        os.mkdir(os.path.join(report.LOGS, "2026-10-04_10-00-00.log"))      # a folder named like a journal
        self.journal("2026-13-45_10-00-00", BROKEN_BOOT)                     # no such day
        outside = self.journal("2026-10-05_10-00-00", BROKEN_BOOT, folder=os.path.join(HOME, "elsewhere"))
        os.symlink(outside, os.path.join(report.LOGS, "2026-10-06_10-00-00.log"))
        got = report.local_updates(10)
        self.assertEqual([os.path.basename(u["log"]) for u in got],
                         ["2026-10-03_10-00-00.log", "2026-10-02_10-00-00.log", "2026-10-01_10-00-00.log"])
        self.assertEqual([u["verdict"] for u in got], [None, None, None])
        self.assertEqual(got[0]["ended"], "2026-10-03T10:00:02")
        report.listing()

    def test_a_long_journal_is_listed_quickly(self):
        build = "".join(f"  CC [M]  drivers/gpu/drm/i915/display/intel_{i:06d}.o\n" for i in range(300_000))
        self.journal("2026-10-31_17-42-38", BROKEN_BOOT.replace("==> The update has been applied\n",
                                                                build + "==> The update has been applied\n"))
        t0 = time.perf_counter()
        u, = report.local_updates()
        self.assertLess(time.perf_counter() - t0, 2)                     # its head and its end are read, not all
        self.assertEqual((u["verdict"], len(u["problems"]), u["mode"], u["packages"]), (2, 3, "auto", 4))
        self.assertGreater(os.path.getsize(u["log"]), report.WHOLE)

    def test_a_report_of_a_journal_is_one_the_relay_files(self):
        self.zone("MSK-3")
        report._who = lambda: WHO
        path = self.journal("2026-10-30_23-58-10", MIDNIGHT)
        r = report.build_from_log(path, ui={"screen": "80x24", "monitors": None})
        self.assertEqual(r["id"], "")                                   # building it makes no ID
        self.assertFalse(os.path.exists(report.SETTINGS))
        r["id"] = report.ensure_id()
        self.assertTrue(relay_takes(r), r["update"])
        self.assertEqual(r["time"], "2026-10-30T21:03:12Z")             # when it ended, in UTC
        u = r["update"]
        self.assertEqual((u["mode"], u["verdict"], u["started"], u["exit"], u["seconds"]), ("manual", 0, True, 0, 302))
        self.assertEqual((u["changed"], u["added"], u["removed"]), (["firefox"], ["libfoo"], ["oldlib", "oldlib2"]))
        self.assertEqual(r["device"]["screen"], "80x24")
        self.assertRegex(r["log"], r"^\[отчёт отправлен вручную \d\d\.\d\d\.20\d\d \d\d:\d\d; устройство и версия "
                                   r"апдейтера в нём — на это время\]\n\[23:58:10 СТАРТ\]")
        self.assertIn("[00:03:12 ПАКЕТЫ] новые: libfoo; удалены: oldlib, oldlib2", r["log"])

    def test_an_update_is_sent_by_hand_with_the_reports_off(self):
        self.zone("UTC0")
        for decided in (False, True):
            with self.subTest(decided=decided):
                shutil.rmtree(os.path.join(HOME, ".local"), ignore_errors=True)
                self.relay()
                report._who = lambda: WHO
                if decided:
                    report.set_consent(False)                           # the installer's question: no
                path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
                self.assertEqual(report.send_now(path), (True, "отчёт отправлен разработчику (№ 1)", 1))
                got, = Relay.got
                self.assertTrue(relay_takes(got), got["update"])
                self.assertEqual((got["update"]["verdict"], got["time"]), (2, "2026-10-31T17:45:46Z"))
                self.assertIn("/boot/initramfs-linux.img пустой — НЕ перезагружайся (сначала: sudo mkinitcpio -P)",
                              got["update"]["problems"])
                self.assertIn("==> ERROR: Initcpio image generation FAILED", got["log"])
                # The machine has an ID now, and that is all: no automatic reports, nor a choice made about them.
                self.assertRegex(report.report_id(), r"^[0-9a-f]{8}$")
                self.assertEqual(got["id"], report.report_id())
                self.assertEqual((report.enabled(), report.decided()), (False, decided))
                self.assertIsNone(report.after_update(self.result()))
                self.assertEqual(self.waiting(), [])
                u, = report.local_updates()
                self.assertEqual(u["issue"], 1)
                self.assertTrue(report._local(u["sent"]))
                self.assertEqual(report._read_json(report.RECORDS)["2026-10-31_17-42-38.log"]["issue"], 1)
                self.assertIn("отправлен", report.listing())

    def test_an_update_sent_again_goes_and_says_it_went_before(self):
        self.relay()
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        self.assertTrue(report.send_now(path)[0])
        rid = report.report_id()
        self.assertEqual(report.send_now(path), (True, "этот отчёт уже у разработчика (№ 1)", 1))
        self.assertEqual(len(Relay.got), 2)
        self.assertEqual({(r["id"], r["time"]) for r in Relay.got}, {(rid, Relay.got[0]["time"])})   # the same report
        u, = report.local_updates()
        self.assertEqual((u["issue"], bool(u["sent"])), (1, True))

    def test_what_the_user_is_told_when_a_report_cannot_go(self):
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        self.assertEqual(report.send_now(path), (False, "сервер для отчётов не подключён", None))
        os.environ["ARCH_UPDATE_REPORT_URL"] = "http://127.0.0.1:9/report"     # nobody listens on port 9
        self.assertEqual(report.send_now(path, timeout=3), (False, "нет связи с сервером — попробуй позже", None))
        self.relay(429, headers={"Retry-After": "1200"})
        ok, message, issue = report.send_now(path)
        self.assertEqual((ok, issue), (False, None))
        self.assertTrue(message.startswith("сервер просит подождать — попробуй позже"), message)
        self.assertIn(f"до {report.PER_HOUR} отчётов в час", message)
        for code, said in ((400, "сервер не принял отчёт (HTTP 400)"), (413, "сервер не принял отчёт (HTTP 413)"),
                           (502, "сервер не принял отчёт (HTTP 502) — попробуй позже"),
                           (408, "нет связи с сервером — попробуй позже")):
            self.relay(code)
            self.assertEqual(report.send_now(path), (False, said, None), code)
        self.assertEqual(report.send_now(os.path.join(report.LOGS, "2026-10-30_10-00-00.log")),
                         (False, "журнал этого обновления не найден", None))
        u, = report.local_updates()
        self.assertEqual((u["sent"], u["issue"]), ("", None))               # none of it counts as sent
        self.assertEqual(report._read_json(report.RECORDS), {})
        self.assertFalse(report.enabled())

    def test_a_report_sent_by_hand_names_nobody(self):
        self.relay()
        report._who = lambda: WHO
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT + PERSONAL_LOG + MORE_LOG)
        self.assertTrue(report.send_now(path)[0])
        got = json.dumps(Relay.got[0], ensure_ascii=False)
        self.assertNotRegex(got, GONE)
        for gone in ("/home/cactus", "3c:22:fb", "ivan.petrov@example.com", "0123456789abcdef0123456789abcdef"):
            self.assertNotIn(gone, got)
        for kept in ("[sudo] password for <user>:", "~/.cache/yay/yay-bin", "gnupg 2.4.9-3→2.4.9-4"):
            self.assertIn(kept, Relay.got[0]["log"])

    def test_a_journal_that_says_not_how_it_ran_is_taken_for_one_typed_by_hand(self):
        # No СТАРТ line (a journal its disk ran out on): what was typed ahead of a password prompt goes all the same.
        self.relay()
        report._who = lambda: WHO
        path = self.journal("2026-10-31_17-42-38", "correct horse battery staple\nhunter2\n"
                                                   "[sudo] password for cactus: \n==> Looking for updates...\n")
        self.assertTrue(report.send_now(path)[0])
        log = Relay.got[0]["log"]
        self.assertNotIn("correct horse", log)
        self.assertNotIn("hunter2", log)
        self.assertIn("[sudo] password for <user>:", log)
        self.assertIsNone(Relay.got[0]["update"]["mode"])

    def test_the_demos_journals_are_never_listed_sent_or_deleted(self):
        self.relay()
        demo = self.journal("2026-11-01_10-00-00", BROKEN_BOOT,
                            folder=os.path.join(report.engine.STATE_DIR, "demo-logs"))
        self.assertEqual(report.local_updates(), [])
        self.assertEqual(report.send_now(demo), (False, "это не журнал обновления этого компьютера", None))
        self.assertFalse(report.delete_local(demo))
        self.assertEqual(Relay.got, [])
        self.assertTrue(os.path.exists(demo))
        self.assertFalse(os.path.exists(report.SETTINGS))                   # no ID either

    def test_an_update_is_deleted_whole_and_nothing_else_is(self):
        self.relay()
        paths = self.journals()
        newest = paths[-1]
        latest = os.path.join(report.LOGS, "latest.log")
        os.symlink(os.path.basename(newest), latest)
        report._write_json(newest[:-4] + ".json", {"verdict": 2})
        self.assertTrue(report.send_now(newest)[0])
        self.assertTrue(report.delete_local(newest))
        for ext in (".log", ".raw", ".json"):
            self.assertFalse(os.path.exists(newest[:-4] + ext), ext)
        self.assertNotIn(os.path.basename(newest), report._read_json(report.RECORDS))
        self.assertEqual(os.readlink(latest), os.path.basename(paths[-2]))  # the newest one left
        self.assertEqual([u["log"] for u in report.local_updates(10)], paths[-2::-1])
        # Anything that is not an update in logs/ is refused, and stays.
        stray = self.result()["log"]
        demo = self.journal("2026-11-01_10-00-00", CLEAN, folder=os.path.join(report.engine.STATE_DIR, "demo-logs"))
        named_like_one = self.journal("2026-11-02_10-00-00", CLEAN, folder=HOME)
        link = os.path.join(report.LOGS, "2026-11-03_10-00-00.log")
        os.symlink(named_like_one, link)
        for log in (stray, demo, named_like_one, link, report.LOGS, os.path.join(report.LOGS, ".."), report.SETTINGS,
                    os.path.join(report.LOGS, "..", "logs", "..", "settings.json"), "", None, 42, "../../etc/passwd"):
            self.assertFalse(report.delete_local(log), log)
        for f in (stray, demo, named_like_one):
            self.assertTrue(os.path.exists(f), f)
        self.assertTrue(os.path.islink(link))
        # An update by its name alone, or as latest.log.
        self.assertTrue(report.delete_local(os.path.basename(paths[0])))
        self.assertTrue(report.delete_local(latest))
        self.assertEqual([u["log"] for u in report.local_updates(10)], paths[-3:0:-1])
        self.assertEqual(os.readlink(latest), os.path.basename(paths[-3]))
        for path in paths[1:-2]:
            self.assertTrue(report.delete_local(path))
        self.assertFalse(os.path.lexists(latest))                           # no journal left to name
        self.assertEqual(sorted(os.listdir(report.LOGS)), ["2026-11-03_10-00-00.log"])

    def test_an_automatic_report_marks_its_update_sent(self):
        self.relay()
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        res = {"verdict": 2, "changed": ["linux"], "added": [], "removed": [], "exitstatus": 0, "problems": ["x"],
               "warnings": [], "ok": [], "log": path}
        queued = report.after_update(res, mode="auto", seconds=188)
        u, = report.local_updates()
        self.assertEqual((u["sent"], u["waiting"]), ("", True))
        self.assertEqual(report.send_pending(), (1, 0))
        u, = report.local_updates()
        self.assertEqual((u["issue"], bool(u["sent"]), u["waiting"]), (1, True, False))
        self.assertEqual(report.state_of(queued), "sent")
        # Sent by hand after that: the same report to the relay (its time too), which has it already.
        self.assertEqual(report.send_now(path), (True, "этот отчёт уже у разработчика (№ 1)", 1))
        self.assertEqual(Relay.got[1]["time"], Relay.got[0]["time"])

    def fixed(self, path, line="[08:05:13 ИСПРАВЛЕНИЕ] по клавише F: собрать заново initramfs ядра linux: не помогло"
                                " — вернул всё как было (код 80)"):
        """What F writes into an update's journal after it ended."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return line

    def test_what_f_did_reaches_the_developer_as_a_report_of_its_own(self):
        # The relay files one log per machine and time: the same time again is a duplicate, and its log stays as
        # it was. What F added since the report went is news: a new time, a log and an issue of its own.
        self.relay()
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        self.assertEqual(report.send_now(path), (True, "отчёт отправлен разработчику (№ 1)", 1))
        line = self.fixed(path)
        self.assertEqual(report.send_now(path), (True, "отчёт отправлен разработчику (№ 2)", 2))
        self.assertNotEqual(Relay.got[1]["time"], Relay.got[0]["time"])
        self.assertIn(line, Relay.got[1]["log"])
        self.assertNotIn(line, Relay.got[0]["log"])
        # Sent again with nothing new: the same report as the second one.
        self.assertEqual(report.send_now(path), (True, "этот отчёт уже у разработчика (№ 2)", 2))
        self.assertEqual(Relay.got[2]["time"], Relay.got[1]["time"])

    def test_with_the_reports_on_what_f_did_goes_by_itself(self):
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        res = {"verdict": 2, "changed": ["linux"], "added": [], "removed": [], "exitstatus": 0, "problems": ["x"],
               "warnings": [], "ok": [], "log": path}
        first = report.after_update(res, mode="auto", seconds=188)
        self.assertFalse(report.follow_up_due(path))                # nothing new yet
        self.assertIsNone(report.after_fix(path))
        line = self.fixed(path)
        self.assertTrue(report.follow_up_due(path))
        self.assertIsNone(report.after_fix(path, demo=True))        # never in the demo
        again = report.after_fix(path)
        self.assertIsNotNone(again)
        # The automatic report that still waited goes as it was (the update's own verdict and problems, which
        # after F may be gone from its summary); the new one goes beside it, with what F did, and says so.
        self.assertEqual(self.waiting(), sorted([os.path.basename(first), os.path.basename(again)]))
        follow = report._read_json(again)
        self.assertIn(line, follow["log"])
        self.assertIn("отчёт после исправления по клавише F", follow["log"])
        self.assertTrue(follow["update"]["ok"][0].startswith("отчёт после исправления по клавише F"),
                        follow["update"]["ok"])
        self.assertFalse(report.follow_up_due(path))                # sent once: not twice
        self.relay()
        self.assertEqual(report.send_pending(), (2, 0))
        self.assertEqual([r["time"] for r in Relay.got], [report._read_json(os.path.join(report.SENT, n))["time"]
                                                          for n in sorted(os.listdir(report.SENT))])
        self.assertIn(follow["time"], [r["time"] for r in Relay.got])
        self.assertEqual(len(Relay.filed), 2)
        u, = report.local_updates()
        # The record names the issue of the newest report, the one after F (queued in the same second here, the two
        # may have gone in either order)
        self.assertEqual((u["issue"], bool(u["sent"]), u["waiting"]),
                         (Relay.filed[(follow["id"], follow["time"])], True, False))

    def test_a_report_taken_back_by_switching_off_never_goes_after_f(self):
        # Reports on, this update's report waits (offline); the user switches the reports off (it is dropped), then
        # on again, then presses F on that update: nothing goes by itself — that report was taken back.
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        report.set_consent(False)
        self.assertEqual(self.waiting(), [])
        report.set_consent(True)
        self.fixed(path)
        self.assertFalse(report.follow_up_due(path))
        self.assertIsNone(report.after_fix(path))
        self.assertEqual(self.waiting(), [])
        self.relay()                                                # S is still the user's to press
        self.assertTrue(report.send_now(path)[0])
        self.fixed(path, "[08:09:00 ИСПРАВЛЕНИЕ] по клавише F: ещё раз")
        self.assertFalse(report.follow_up_due(path))                # and taken back it stays

    def test_a_follow_up_that_cannot_be_written_leaves_the_waiting_report(self):
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        first = report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        self.fixed(path)
        real = report._write_json

        def full(where, data):
            if os.path.dirname(where) == report.OUTBOX:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(where, data)
        report._write_json = full
        try:
            self.assertIsNone(report.after_fix(path))
        finally:
            report._write_json = real
        self.assertEqual(self.waiting(), [os.path.basename(first)])  # the update's own report is still there

    def test_s_pressed_while_one_is_on_its_way_after_f_is_news(self):
        # S, then F while S's answer is still coming, then S again: the second waits for the first, knows what it
        # had, and goes as a report of its own (before, it took the first one's time and was a duplicate).
        def slow(body):
            time.sleep(1.5)
            return 201
        self.relay(slow)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        first = []
        t = threading.Thread(target=lambda: first.append(report.send_now(path)))
        t.start()
        time.sleep(0.4)
        line = self.fixed(path)
        second = report.send_now(path)
        t.join()
        self.assertEqual(first, [(True, "отчёт отправлен разработчику (№ 1)", 1)])
        self.assertEqual(second, (True, "отчёт отправлен разработчику (№ 2)", 2))
        self.assertIn(line, Relay.got[1]["log"])
        self.assertNotEqual(Relay.got[0]["time"], Relay.got[1]["time"])

    def test_a_try_again_after_a_lost_answer_is_the_same_report(self):
        # The relay files a report even when the answer never reaches the machine: trying again is the same
        # report (the time comes from the journal, not the clock), not a second issue.
        self.relay()
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        self.assertTrue(report.send_now(path)[0])
        self.fixed(path)
        Relay.lose = 1
        self.assertEqual(report.send_now(path), (False, "нет связи с сервером — попробуй позже", None))
        time.sleep(1.1)                         # a second later by the clock: still the same report
        self.assertEqual(report.send_now(path), (True, "этот отчёт уже у разработчика (№ 2)", 2))
        self.assertEqual(len(Relay.filed), 2)

    def test_the_list_says_a_newer_report_waits(self):
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        self.relay()
        self.assertEqual(report.send_pending(), (1, 0))
        self.fixed(path)
        self.assertIsNotNone(report.after_fix(path))
        self.relay(503)                                             # the one after F cannot go yet
        self.assertEqual(report.send_pending(), (0, 1))
        u, = report.local_updates()
        self.assertEqual((u["issue"], bool(u["sent"]), u["waiting"]), (1, True, True))
        self.assertIn("новый ждёт отправки", report.listing())

    def test_a_record_of_the_version_before_counts_from_when_its_report_was_made(self):
        # Its automatic report waited, F wrote to the journal, then the report went out: what the relay has was
        # taken when it was queued, so F's lines are news, although the record says it went after them.
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        records = report._read_json(report.RECORDS)
        rec = records["2026-10-31_17-42-38.log"]
        del rec["size"]
        queued_at = report._queued_at(rec["queued"]).timestamp()
        rec["sent"] = datetime.datetime.fromtimestamp(queued_at + 3600).isoformat(timespec="seconds")
        report._write_json(report.RECORDS, records)
        os.utime(path, (queued_at + 600, queued_at + 600))
        self.assertTrue(report.follow_up_due(path))

    def test_a_sender_waits_for_another_and_sends_what_was_queued_meanwhile(self):
        report.set_consent(True)
        self.relay()
        os.makedirs(report.REPORTS, exist_ok=True)
        held = open(os.path.join(report.REPORTS, "send.lock"), "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        threading.Timer(1.0, held.close).start()                    # another sender, still at its last report
        report.queue(report.build(self.result()))                   # queued meanwhile (F's report, say)
        self.assertEqual(report.send_pending(), (1, 0))

    def test_what_f_did_goes_again_after_an_automatic_report_that_went(self):
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        self.relay()
        self.assertEqual(report.send_pending(), (1, 0))
        self.fixed(path)
        report.after_fix(path)
        self.assertEqual(report.send_pending(), (1, 0))
        self.assertEqual(len(Relay.filed), 2)                       # two logs, two issues: the second has F's lines
        self.assertNotEqual(Relay.got[0]["time"], Relay.got[1]["time"])

    def test_no_report_goes_by_itself_for_an_update_that_had_none(self):
        # The consent is for each update's report: an update from before it, or switched off, gets none from F.
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        self.fixed(path)
        self.assertFalse(report.follow_up_due(path))
        report.set_consent(True)
        self.assertFalse(report.follow_up_due(path))
        self.assertIsNone(report.after_fix(path))
        self.assertEqual(self.waiting(), [])

    def test_a_record_of_the_version_before_tells_news_by_time(self):
        # Records kept no size before: a journal written to well after its report was queued has news.
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        records = report._read_json(report.RECORDS)
        rec = records["2026-10-31_17-42-38.log"]
        del rec["size"]
        report._write_json(report.RECORDS, records)
        queued_at = report._queued_at(rec["queued"]).timestamp()
        os.utime(path, (queued_at + 1, queued_at + 1))
        self.assertFalse(report.follow_up_due(path))
        os.utime(path, (queued_at + 60, queued_at + 60))
        self.assertTrue(report.follow_up_due(path))

    def test_a_report_sent_by_hand_takes_the_waiting_one_along(self):
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        queued = report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        self.assertEqual(report.state_of(queued), "waiting")                # no relay yet
        self.relay()
        self.assertEqual(report.send_now(path), (True, "отчёт отправлен разработчику (№ 1)", 1))
        self.assertEqual(report.state_of(queued), "")                       # it went out by hand
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(len(Relay.got), 1)
        self.assertEqual(report._read_json(report.RECORDS)["2026-10-31_17-42-38.log"]["time"], Relay.got[0]["time"])

    def test_the_update_the_menu_warns_about_stays(self):
        # Red in this very boot, nothing fixed: deleting it would end the menu's «НЕ перезагружайся».
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(self.boot_began + 600))
        summary = {"verdict": 2, "problems": ["нет /boot/initramfs-linux.img — НЕ перезагружайся"], "started": True,
                   "boot_id": report.engine.boot_id()}
        path = self.journal(stamp, BROKEN_BOOT, summary)
        self.assertTrue(report.is_unresolved(path))
        self.assertFalse(report.delete_local(path))
        self.assertTrue(os.path.exists(path) and os.path.exists(path[:-4] + ".json"))
        # one without a summary (a full disk did not let it be written): its journal says it all the same
        os.remove(path[:-4] + ".json")
        self.assertFalse(report.delete_local(path))
        # fixed and looked at again (or rebooted since): it goes
        report._write_json(path[:-4] + ".json", dict(summary, verdict=0, problems=[], resolved="2026-11-03T13:00:00"))
        self.assertTrue(report.delete_local(path))
        self.assertFalse(os.path.exists(path))

    def test_a_deleted_update_leaves_no_report_behind(self):
        self.relay()
        report.set_consent(True)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        report.after_update({"verdict": 2, "log": path, "problems": ["x"]}, mode="auto")
        self.assertEqual(report.send_pending(), (1, 0))
        self.assertIn("Initcpio image generation FAILED", report.preview())
        refused = os.path.join(report.REJECTED, "20261031-174600-beef.json")      # one the relay refused, before
        report._write_json(refused, {"schema": 1, "log": BROKEN_BOOT})
        self.assertTrue(report.delete_local(path))
        self.assertEqual(os.listdir(report.SENT), [])
        self.assertFalse(os.path.exists(refused))
        self.assertNotIn("Initcpio image generation FAILED", report.preview())

    def test_reports_the_version_before_sent_by_itself_count_as_sent(self):
        # 152651e sent the automatic reports and kept no record of which update each was of.
        self.zone("UTC0")
        self.relay()
        older = self.journal("2026-10-30_21-00-00", WARNED)
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        old = {"schema": 1, "id": "1a2b3c4d", "time": "2026-10-31T17:46:01Z", "log": BROKEN_BOOT,
               "update": {"verdict": 2}}
        report._write_json(os.path.join(report.SETTINGS), {"reports": True, "report_id": "1a2b3c4d"})
        report._write_json(os.path.join(report.SENT, "20261031-174601-abcd.json"), old)
        Relay.filed[("1a2b3c4d", "2026-10-31T17:46:01Z")] = 7                  # the relay filed it as № 7
        newest, before = report.local_updates()
        self.assertTrue(newest["sent"])
        self.assertEqual(before["sent"], "")                                    # not the one before it
        self.assertEqual(report.report_state(path)["sent"], newest["sent"])
        self.assertEqual(report.send_now(path), (True, "этот отчёт уже у разработчика (№ 7)", 7))
        self.assertEqual(Relay.got[0]["time"], "2026-10-31T17:46:01Z")
        self.assertTrue(report.delete_local(older))
        self.assertTrue(os.path.exists(os.path.join(report.SENT, "20261031-174601-abcd.json")))   # not its report

    def test_an_account_renamed_since_is_taken_out_too(self):
        self.relay()
        report._who = lambda: WHO                                              # the account is "cactus" now
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT + "[sudo] password for vanya:\n"
                                                    "  -> /home/vanya/.cache/yay/brave-bin\n"
                                                    "doas (vanya@vanya-chromebook) password:\n"
                                                    "vanya-chromebook kernel: usb 1-1: new device\n"
                                                    "  -> BUILDDIR=/home/build/makepkg\n==> Starting build()...\n")
        self.assertTrue(report.send_now(path)[0])
        log = Relay.got[0]["log"]
        self.assertNotIn("vanya", log)
        for kept in ("[sudo] password for <user>:", "/home/<user>/.cache/yay/brave-bin", "(<user>@<host>) password:",
                     "BUILDDIR=/home/<user>/makepkg", "==> Starting build()"):   # a folder's name is no word to hide
            self.assertIn(kept, log)

    def test_a_send_that_failed_leaves_no_id(self):
        path = self.journal("2026-10-31_17-42-38", BROKEN_BOOT)
        os.environ["ARCH_UPDATE_REPORT_URL"] = "http://127.0.0.1:9/report"     # nobody listens on port 9
        self.assertFalse(report.send_now(path, timeout=3)[0])
        self.relay(503)
        self.assertFalse(report.send_now(path)[0])
        self.assertEqual(report.status()["id"], "")
        self.assertFalse(os.path.exists(report.SETTINGS))
        tried = {r["id"] for r in Relay.got}
        self.relay()
        self.assertTrue(report.send_now(path)[0])
        self.assertEqual({Relay.got[0]["id"]}, tried)                          # the same ID it tried with
        self.assertEqual(report.report_id(), Relay.got[0]["id"])

    def test_settings_keep_each_other(self):
        self.assertEqual(report.option("restart_services", False), False)   # nothing written yet
        report.set_option("restart_services", True)                         # makes the state folder
        self.assertIs(report.option("restart_services", False), True)
        report.set_consent(True)
        rid = report.report_id()
        report.set_option("restart_services", False)
        self.assertEqual((report.option("restart_services", True), report.enabled(), report.report_id()),
                         (False, True, rid))
        report._write_json(report.SETTINGS, dict(report.settings(), restart_services="yes"))   # by hand
        self.assertIs(report.option("restart_services", False), False)
        self.assertEqual((report.option("missing"), report.option("missing", 3)), (None, 3))
        # The reports' own switch, through set_consent: off drops what waits to be sent.
        report.after_update(self.result())
        self.assertEqual(len(self.waiting()), 1)
        report.set_option("reports", False)
        self.assertEqual((self.waiting(), report.enabled(), report.report_id(), report.settings()["restart_services"]),
                         ([], False, rid, "yes"))
        self.assertEqual(report.ensure_id(), rid)                           # an ID it has is kept

    def test_the_command_line_lists_and_sends_the_last_updates(self):
        paths = self.journals()
        env = dict(os.environ, HOME=HOME)
        cli = [sys.executable, os.path.join(ROOT, "lib", "report.py")]
        out = subprocess.run(cli + ["--list"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, r"(?m)^ 1  31\.10\.2026 17:42  ✗ нужна проверка +4 пакета +не отправлен\n"
                                     r"    /boot/initramfs-linux\.img пустой — НЕ перезагружайся")
        for line in (" 2  30.10.2026 23:58  ✓ всё чисто", " 3  30.10.2026 21:00  ⚠ есть что прочитать",
                     " 4  30.10.2026 08:00  · не начиналось", " 5  29.10.2026 07:00  ✗ не начиналось"):
            self.assertIn(line, out.stdout)
        self.assertNotIn("28.10.2026", out.stdout)                          # the newest five only
        self.relay()
        env["ARCH_UPDATE_REPORT_URL"] = os.environ["ARCH_UPDATE_REPORT_URL"]
        sent = subprocess.run(cli + ["--send", "1"], env=env, capture_output=True, text=True)
        self.assertEqual((sent.returncode, sent.stdout.splitlines()),
                         (0, ["Отправляю отчёт об обновлении 31.10.2026 17:42…", "отчёт отправлен разработчику (№ 1)"]))
        out = subprocess.run(cli + ["--list"], env=env, capture_output=True, text=True).stdout
        self.assertRegex(out, r"(?m)^ 1  .* отправлен \d\d\.\d\d \d\d:\d\d \(№ 1\)$")
        sent = subprocess.run(cli + ["--send-log", paths[0]], env=env, capture_output=True, text=True)
        self.assertEqual((sent.returncode, sent.stdout.splitlines()[-1]), (0, "отчёт отправлен разработчику (№ 2)"))
        for bad in (["--send", "0"], ["--send", "8"], ["--send", "x"], ["--send", "²"],
                    ["--send-log", os.path.join(HOME, "nothing.log")], ["--send-log", self.result()["log"]]):
            run = subprocess.run(cli + bad, env=env, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0, bad)
            self.assertTrue(run.stderr.strip(), bad)
        env["ARCH_UPDATE_REPORT_URL"] = ""
        run = subprocess.run(cli + ["--send", "2"], env=env, capture_output=True, text=True)
        self.assertEqual((run.returncode, run.stderr.strip()), (1, "сервер для отчётов не подключён"))
        self.assertEqual(len(Relay.got), 2)
        self.assertFalse(report.enabled())
        shutil.rmtree(report.LOGS)
        out = subprocess.run(cli + ["--list"], env=env, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "На этом компьютере пока нет журналов обновлений.")

    # ── the installer's side

    def test_the_command_line(self):
        env = dict(os.environ, HOME=HOME)
        cli = [sys.executable, os.path.join(ROOT, "lib", "report.py")]
        self.assertEqual(subprocess.run(cli + ["--decided"], env=env).returncode, 1)
        self.assertEqual(subprocess.run(cli + ["--consent", "yes"], env=env).returncode, 0)
        self.assertEqual(subprocess.run(cli + ["--decided"], env=env).returncode, 0)
        out = subprocess.run(cli + ["--status"], env=env, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "включены")
        subprocess.run(cli + ["--consent", "no"], env=env)
        out = subprocess.run(cli + ["--status"], env=env, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), "выключены")
        preview = subprocess.run(cli + ["--preview"], env=env, capture_output=True, text=True).stdout
        self.assertNotIn(socket.gethostname(), preview) if len(socket.gethostname()) >= 3 and \
            socket.gethostname().lower() not in report.COMMON else None
        self.assertIn('"device"', preview)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
