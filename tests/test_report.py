#!/usr/bin/env python3
"""Tests of the update reports (lib/report.py): nothing is collected or sent without the user's
consent or in the demo, everything personal is taken out of a report, and a report that cannot be
sent waits for the next try. A local HTTP server stands in for the relay; HOME is a temporary
folder, so the real settings and outbox are never touched.

  python3 tests/test_report.py
"""
import errno
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


class Relay(http.server.BaseHTTPRequestHandler):
    """The relay's stand-in: answers every POST with Relay.code (a number, or a function of the
    report that came) and keeps what it got."""
    code, got = 201, []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Relay.got.append(body)
        self.send_response(Relay.code(body) if callable(Relay.code) else Relay.code)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


class Reports(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(HOME, ".local"), ignore_errors=True)
        self.saved_who, self.saved_sender = report._who, report.start_sender
        report.start_sender = lambda: None            # the tests send by hand, in this process
        os.environ["ARCH_UPDATE_REPORT_URL"] = ""     # never the real relay: a test that sends sets its own
        self.server = None

    def tearDown(self):
        report._who, report.start_sender = self.saved_who, self.saved_sender
        os.environ.pop("ARCH_UPDATE_REPORT_URL", None)
        self.stop_relay()

    def relay(self, code=201):
        self.stop_relay()
        Relay.code, Relay.got = code, []
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
