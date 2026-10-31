#!/usr/bin/env python3
"""Tests of the update reports (lib/report.py): nothing is collected or sent without the user's
consent or in the demo, everything personal is taken out of a report, and a report that cannot be
sent waits for the next try. A local HTTP server stands in for the relay; HOME is a temporary
folder, so the real settings and outbox are never touched.

  python3 tests/test_report.py
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
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
WHO = ({"cactus"}, {"cactus-chromebook", "cactus-chromebook.local"}, {"Ivan", "Petrov"}, {"/home/cactus"},
       "0123456789abcdef0123456789abcdef")


class Relay(http.server.BaseHTTPRequestHandler):
    """The relay's stand-in: answers every POST with Relay.code and keeps what it got."""
    code, got = 201, []

    def do_POST(self):
        Relay.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(Relay.code)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


class Reports(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(os.path.join(HOME, ".local"), ignore_errors=True)
        self.saved_who, self.saved_sender = report._who, report.start_sender
        report.start_sender = lambda: None            # the tests send by hand, in this process
        self.server = None

    def tearDown(self):
        report._who, report.start_sender = self.saved_who, self.saved_sender
        os.environ.pop("ARCH_UPDATE_REPORT_URL", None)
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def relay(self, code=201):
        Relay.code, Relay.got = code, []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["ARCH_UPDATE_REPORT_URL"] = f"http://127.0.0.1:{self.server.server_port}/report"

    def result(self, log_text=PERSONAL_LOG):
        path = os.path.join(HOME, "update.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(log_text)
        return {"verdict": 1, "changed": ["brave-bin", "gnupg"], "added": [], "removed": [], "exitstatus": 0,
                "problems": [], "warnings": ["есть пакеты-сироты"], "ok": ["pacman завершился штатно"], "log": path}

    def waiting(self):
        return sorted(os.listdir(report.OUTBOX)) if os.path.isdir(report.OUTBOX) else []

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

    # ── what a report holds

    def test_everything_personal_is_taken_out_of_the_log(self):
        report._who = lambda: WHO
        text = report.scrubber()(PERSONAL_LOG)
        for gone in ("cactus", "Ivan", "Petrov", "3c:22:fb", "192.168.1.20", "10.0.0.1", "fe80::",
                     "ivan.petrov@example.com", "0123456789abcdef0123456789abcdef"):
            self.assertNotIn(gone, text)
        for kept in ("password for <user>:", "~/.cache/yay/yay-bin", "brave-bin 1:1.97.57-1→1:1.98.63-1",
                     "gnupg 2.4.9-3→2.4.9-4", "archlinux-keyring 20261001-1", "7.2.11.arch1-1 -> 7.2.12.arch1-1",
                     "[17:42:38 СТАРТ]", "[2026-10-30T17:42:47-0700]", "getty@tty1.service user@1000.service",
                     "3B94A80E50A477C7"):
            self.assertIn(kept, text)

    def test_names_that_are_common_words_leave_the_log_readable(self):
        report._who = lambda: ({"arch"}, {"archlinux"}, set(), {"/home/arch"}, "")
        text = report.scrubber()("Arch Linux: arch-update, archlinux-keyring; [sudo] password for arch:\n"
                                 "/home/arch/.cache/yay/x and archlinux.org\n")
        self.assertIn("Arch Linux: arch-update, archlinux-keyring", text)
        self.assertIn("password for <user>:", text)
        self.assertIn("~/.cache/yay/x and archlinux.org", text)

    def test_a_report_of_this_machine_names_nobody(self):
        # the real names of the machine the test runs on, wherever they turn up
        users, hosts, names, homes, machine = report._who()
        log = PERSONAL_LOG + "".join(f"\n{u} {u}@{h} {home}/x" for u in users for h in hosts for home in homes)
        report.set_consent(True)
        text = json.dumps(report.build(self.result(log), mode="auto", seconds=187,
                                       ui={"screen": "80x24", "monitors": 1}), ensure_ascii=False)
        for word in users | hosts | names:
            if len(word) >= 3 and word.lower() not in report.COMMON:
                self.assertNotRegex(text, r"(?i)(?<![\w.-])" + __import__("re").escape(word) + r"(?![\w.-])")
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

    def test_a_refused_report_is_set_aside_not_retried(self):
        self.relay(400)
        report.set_consent(True)
        path = report.after_update(self.result())
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(report.state_of(path), "rejected")

    def test_without_a_relay_reports_wait(self):
        report.set_consent(True)
        report.after_update(self.result())
        self.assertFalse(report.status()["endpoint"]) if not report.ENDPOINT else None
        if not report.ENDPOINT:
            self.assertEqual(report.send_pending(), (0, 1))

    def test_nothing_is_sent_once_consent_is_withdrawn(self):
        self.relay()
        report.set_consent(True)
        report.after_update(self.result())
        report.set_consent(False)
        self.assertEqual(report.send_pending(), (0, 0))
        self.assertEqual(Relay.got, [])

    def test_the_outbox_never_grows_without_end(self):
        report.set_consent(True)
        for _ in range(report.KEEP[report.OUTBOX] + 5):
            report.queue({"schema": 1})
        self.assertEqual(len(self.waiting()), report.KEEP[report.OUTBOX])

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
