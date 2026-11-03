#!/usr/bin/env python3
"""Headless end-to-end test: drive `arch-update-ui --demo` at laptop and desktop terminal sizes.

Every run gets its own empty HOME (the real one is never touched) and goes through the whole
demo in a real pty: splash, menu, the auto mode dashboard, all three mini-games, the question
modal, a game left open until the update finishes, the summary (where S, which sends the report
by hand, must only say that the demo does not do it). Game and menu keys are also typed with the
Russian layout on (з pauses Tetris; a short extra run leaves the menu with й).
The 136x38 run starts with the update reports on, one report from earlier waiting and a server
address where nobody listens: the demo must still queue, send and mention none.
Two more short runs (80x24, 200x60) open the settings with Enter pressed twice and held down (the
reports must stay off, the hint that pointed there must be gone), switch them on, look at "Что именно
уходит", switch them off again (the machine's ID must still be shown, for whoever wants their
reports deleted), switch restarting the services on and off, open the last updates (none yet) and
check that nothing was queued or sent.
Two runs (80x24, 200x60) start without --demo, with a stand-in arch-update and sudo and no network,
on journals of three updates made up in their HOME (one from before the summaries): the list of the
last updates, one report, sending one by hand to a closed local port (never the real server for
the reports) and deleting one with D pressed twice.
Two demo runs (80x24, 200x60) start with a summary in the demo's journals that said not to reboot
in this very boot: the menu warns, its keys and the summary opened again from it only say that the
demo does not do that, and nothing of it changes.
One run (80x24) without --demo starts with such a summary of its own and a stand-in update that leaves
a service waiting: F under the menu's warning and R on the summary go to the plain terminal (where
the stand-in sudo only says what it would run) and back with Enter, and what came of them is kept.
One run (80x24) without --demo has a stand-in for the server for the reports that answers after two seconds
(never the real server): S under the menu's warning, then the summary opened from the menu before the answer came
(it says how the report fared, no toast left over); then an update, S on its summary and Enter at once (the program
waits for the answer, says it, then closes). Both reports are recorded with their issues; nothing is switched on.
One run (80x24) takes "Обычный Arch-Update" and types a password ahead while the update gets ready
(a stand-in pacman makes that take two seconds): it must reach neither the screen nor the journal.
The output is rendered with pyte; the test fails on a UI exception, a screen that never shows
up, or a bad exit. A text snapshot of every stage is written to tests/out/<cols>x<rows>/.

  python3 tests/run.py              every size, in parallel
  python3 tests/run.py 80x24 ...    only these sizes

Needs python-pexpect and python-pyte (pip: pexpect pyte).
"""
import glob
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

import pexpect
import pyte

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
UI = os.path.join(ROOT, "bin", "arch-update-ui")
OUT = os.path.join(ROOT, "tests", "out")
SIZES = [(80, 24), (100, 30), (136, 38), (200, 60)]
UP, DOWN, ESC, ENTER = "\x1b[A", "\x1b[B", "\x1b", "\r"
REPORTS_ON = (136, 38)          # the play run at this size has the update reports on
EARLIER_REPORT = "20261030-120000-abcd.json"     # waits in that run's outbox from the start
TYPED_AHEAD = "secret-typed-ahead"               # the manual run's password, typed before anything asks
DEMO_SAYS = "в демо это не делается"
STATE = ".local/state/arch-update-auto"
# What the summary says of this update's report, when it says anything (never in the demo)
REPORT_LINES = ("отчёт отправлен", "отчёт ждёт", "отчёт сохранён", "отчёт: ", "не принят сервером")
# The updates of the runs without --demo: journals of three updates, newest last, as the engine leaves them:
# one from before there were summaries (its own lines say what came of it), two with a summary next to them.
OLD_JOURNAL = """[09:15:00 СТАРТ] arch-update, режим: автоответы, новости: не останавливаться, вид: LiveView
==> The update has been applied
[09:20:44 КОНЕЦ] arch-update завершился, код 0
[09:20:45 ПАКЕТЫ] обновлены: linux 6.18.1.arch1-1→6.18.2.arch1-1, mesa 1:26.2.5-1→1:26.2.6-1
[09:20:45 ПРОБЛЕМА] нет /boot/initramfs-linux.img — НЕ перезагружайся, система не загрузится
[09:20:45 ВНИМАНИЕ] есть пакеты-сироты — не удалял, посмотреть: pacman -Qdtq
"""
UPDATES = [("2026-11-01_09-15-00", OLD_JOURNAL, None),
           ("2026-11-02_18-30-00", "[18:30:00 СТАРТ] arch-update, режим: автоответы\n",
            {"verdict": 1, "changed": ["cups"], "added": [], "removed": [], "problems": [],
             "warnings": ["сервис ждёт перезапуска: cups"], "notes": [], "ok": ["pacman завершился штатно"],
             "exitstatus": 0, "started": True, "mode": "auto", "began": "2026-11-02T18:30:00",
             "ended": "2026-11-02T18:31:10", "boot_id": "an-earlier-boot",
             "services": {"listed": ["cups.service"], "restarted": [], "skipped": ["cups.service"],
                          "waiting": ["cups.service"], "later": [], "failed": []}, "fixes": [], "boot": []}),
           ("2026-11-03_08-00-00", "[08:00:00 СТАРТ] arch-update, режим: ручной\n",
            {"verdict": 0, "changed": ["mesa", "vulkan-radeon"], "added": [], "removed": [], "problems": [],
             "warnings": [], "notes": ["перезагрузись, когда будет удобно (обновились: mesa)"],
             "ok": ["pacman завершился штатно"], "exitstatus": 0, "started": True, "mode": "manual",
             "began": "2026-11-03T08:00:00", "ended": "2026-11-03T08:02:00", "boot_id": "an-earlier-boot",
             "services": {}, "fixes": [], "boot": []})]
# The last update, in this very boot: it said not to reboot, nothing has fixed it (the demo's, and the
# real program's in the run with stand-ins). It began an hour before the run: an update the run makes is newer.
BAD_BOOT = [{"kind": "image", "pkg": "linux", "path": "/boot/initramfs-linux.img", "how": "missing",
             "text": "нет /boot/initramfs-linux.img — НЕ перезагружайся, система не загрузится"}]
# Stand-ins for the runs without --demo. sudo says what it would run and runs nothing (sudo -n true, as
# the engine asks whether it wants a password, says no). arch-update has no update to offer, or one whose
# run leaves a service waiting to be restarted (as arch-update 4.4's restart_services.sh asks, unanswered).
SUDO = """while [ "${1#-}" != "$1" ]; do shift; done
[ $# -eq 0 ] || echo "[sudo в тесте] $*"
exit 0"""
NO_UPDATE = "exit 7"
ONE_UPDATE = """if [ "$1" = -l ]; then
    printf '==> Packages:\\ncups 2.4.14-1 -> 2.4.15-1\\n'
    exit 0
fi
printf '==> Updating Packages...\\n==> Services:\\nThe following service requires a post upgrade restart\\n\\n'
printf '1 - cups.service\\n\\n'
read -rp '-> Select the service(s) to restart (e.g. 1 3 5), select 0 to restart them all or press "enter" \
to continue without restarting the service(s): ' ans
printf '\\n==> WARNING: The service(s) restart has not been performed\\n==> The update has been applied\\n'"""


def closed_port():
    """A free local port, closed again: connections to it are refused at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


class StandInRelay:
    """A stand-in for the server for the reports, on this machine (never the real one): every report gets 201 and the
    next issue's number, from 7 on, after a pause long enough for the program to be left meanwhile."""

    def __init__(self, delay=2.0):
        self.got, self.delay = [], delay
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                relay.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                time.sleep(relay.delay)
                answer = json.dumps({"ok": True, "issue": 6 + len(relay.got)}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/report"


class Run:
    def __init__(self, cols, rows, name=None, reports_on=False, slow_pacman=False, demo=True, seed=None,
                 update=NO_UPDATE, relay=None):
        self.cols, self.rows = cols, rows
        self.reports_on = reports_on
        self.name = name or f"{cols}x{rows}"
        self.home = tempfile.mkdtemp(prefix=f"arch-update-test-{self.name}-")
        self.state = os.path.join(self.home, STATE)
        self.dir = os.path.join(OUT, self.name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.shots = 0
        self.failures = []
        self.after = []                 # checks of what the run left in its HOME, once it has ended
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "ARCH_UPDATE"))}
        env.update(HOME=self.home, TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        # No run reaches the real server for the update reports, whatever lib/report.py has as ENDPOINT.
        env["ARCH_UPDATE_REPORT_URL"] = self.seed_reports() if reports_on else ""
        stubs = {}
        if slow_pacman:                 # pacman -Q on a slow machine: the update takes a moment to start
            stubs["pacman"] = "sleep 2"
        if not demo:
            # Without --demo the program is the real one: arch-update and sudo are stand-ins (nothing is
            # updated or elevated), and nothing is reached on the network: archlinux.org's news through a proxy
            # where nobody listens, the reports a closed local port, never the real server for them.
            stubs.update({"arch-update": update, "sudo": SUDO})
            proxy = f"http://127.0.0.1:{closed_port()}"
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
                env[k] = proxy
            env["no_proxy"] = env["NO_PROXY"] = "127.0.0.1,localhost"
            env["ARCH_UPDATE_REPORT_URL"] = relay.url if relay else f"http://127.0.0.1:{closed_port()}/report"
            env["ARCH_UPDATE_AUTO_NOTIFY"] = "0"
        self.relay = relay
        if stubs:
            stub = os.path.join(self.home, "stub")
            os.makedirs(stub)
            for name, body in stubs.items():
                with open(os.path.join(stub, name), "w") as f:
                    f.write(f"#!/bin/bash\n{body}\n")
                os.chmod(os.path.join(stub, name), 0o755)
            env["PATH"] = stub + os.pathsep + env.get("PATH", os.defpath)
        if seed:
            seed(self)
        self.child = pexpect.spawn(sys.executable, [UI] + (["--demo"] if demo else []), env=env,
                                   dimensions=(rows, cols))
        # The UI blocks once the pty buffer is full, so somebody must read all the time.
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()

    def seed_reports(self):
        """The reports on, one from an earlier update still waiting, and a server address where nobody
        listens (returned): whatever tried to send would leave its error in reports/status.json."""
        state = os.path.join(self.home, ".local/state/arch-update-auto")
        os.makedirs(os.path.join(state, "reports", "outbox"))
        with open(os.path.join(state, "settings.json"), "w") as f:
            json.dump({"reports": True, "reports_decided": "2026-10-30", "report_id": "1a2b3c4d"}, f)
        with open(os.path.join(state, "reports", "outbox", EARLIER_REPORT), "w") as f:
            json.dump({"schema": 1, "id": "1a2b3c4d", "time": "2026-10-30T12:00:00Z"}, f)
        self.after.append(self.demo_sent_nothing)
        return f"http://127.0.0.1:{closed_port()}/report"

    def seed_updates(self):
        """The journals (and summaries) of UPDATES in logs/, a .raw beside the oldest."""
        logs = os.path.join(self.state, "logs")
        os.makedirs(logs)
        for stamp, journal, summary in UPDATES:
            with open(os.path.join(logs, stamp + ".log"), "w", encoding="utf-8") as f:
                f.write(journal)
            if summary:
                with open(os.path.join(logs, stamp + ".json"), "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False)
        with open(os.path.join(logs, UPDATES[0][0] + ".raw"), "wb") as f:
            f.write(OLD_JOURNAL.encode())

    def seed_bad(self, folder="demo-logs"):
        """The last update (the demo's, in demo-logs) said not to reboot, in this boot, and left a service
        waiting."""
        logs = os.path.join(self.state, folder)
        os.makedirs(logs)
        began = time.time() - 3600
        self.bad_stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(began))
        with open(os.path.join(logs, self.bad_stamp + ".log"), "w", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S СТАРТ] lib/demo-update, режим: автоответы\n", time.localtime(began)))
        self.bad = {"verdict": 2, "changed": ["linux"], "added": [], "removed": [], "problems": [BAD_BOOT[0]["text"]],
                    "warnings": ["сервис ждёт перезапуска: cups"], "notes": [], "ok": ["pacman завершился штатно"],
                    "exitstatus": 0, "started": True, "mode": "auto",
                    "began": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(began)),
                    "ended": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(began + 180)),
                    "boot_id": boot_id(), "boot": BAD_BOOT, "reboot": True,
                    "services": {"listed": ["cups.service"], "restarted": [], "skipped": ["cups.service"],
                                 "waiting": ["cups.service"], "later": [], "failed": []},
                    "fixes": [{"key": "initramfs", "label": "собрать initramfs заново (mkinitcpio -P)",
                               "argv": ["sudo", "mkinitcpio", "-P"]}]}
        with open(os.path.join(logs, self.bad_stamp + ".json"), "w", encoding="utf-8") as f:
            json.dump(self.bad, f, ensure_ascii=False)

    def _drain(self):
        """The only thread that reads from the program and asks whether it still runs: two threads asking
        (waitpid) at once, one of them finds no child any more. It returns once the program has ended."""
        while True:
            try:
                data = self.child.read_nonblocking(65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.ExceptionPexpect, OSError, ValueError):     # EOF among them
                return
            with self.lock:
                self.stream.feed(data)

    def running(self):
        return self.reader.is_alive()

    def text(self):
        with self.lock:
            return "\n".join(line.rstrip() for line in self.screen.display)

    def unpainted(self, stage):
        """Cells left in the terminal's own background colour (a light theme would show there)."""
        with self.lock:
            n = sum(1 for row in self.screen.buffer.values() for cell in row.values() if cell.bg == "default")
            n += sum(self.cols - len(row) for row in self.screen.buffer.values())
            n += (self.rows - len(self.screen.buffer)) * self.cols
        if n:
            self.failures.append(f"{stage}: {n} клеток без нашего фона")

    def snapshot(self, stage):
        self.shots += 1
        with open(os.path.join(self.dir, f"{self.shots:02d}-{stage}.txt"), "w") as f:
            f.write(self.text() + "\n")

    def wait(self, stage, *needles, timeout=30):
        """Wait until any of the needles is on screen; record a snapshot either way."""
        end = time.time() + timeout
        while time.time() < end:
            screen = self.text()
            if any(n in screen for n in needles):
                self.snapshot(stage)
                return True
            if not self.running():
                break
            time.sleep(0.1)
        self.snapshot(stage + "-FAILED")
        self.failures.append(f"{stage}: не появилось {' / '.join(map(repr, needles))}")
        return False

    def send(self, *keys, pause=0.25):
        for k in keys:
            self.child.send(k)
            time.sleep(pause)

    def resize(self, cols, rows):
        with self.lock:
            self.screen.resize(rows, cols)
        self.child.setwinsize(rows, cols)

    def play(self):
        if not self.wait("splash", "by Cactus", timeout=10):
            return
        if not self.wait("menu", "Обновить автоматически", timeout=30):
            return
        self.unpainted("menu")
        if self.reports_on and "отчёты об обновлениях: вкл" not in self.text():
            self.failures.append("меню: нет подписи «отчёты об обновлениях: вкл»")
        self.send(ENTER)
        if not self.wait("dashboard", "мини-игры", timeout=20):
            return
        time.sleep(0.5)
        self.unpainted("dashboard")
        if self.name == "136x38":                      # live resize on the dashboard
            self.resize(90, 26)
            time.sleep(0.6)
            self.wait("dashboard-resized", "мини-игры", timeout=5)
            self.resize(self.cols, self.rows)
            time.sleep(0.6)
        for number, title, moves in (("1", "СЛЕДУЮЩАЯ", ["\x1b[D", "\x1b[A", " "]),
                                     ("2", "ЗМЕЙКА", ["\x1b[A", "\x1b[D"]),
                                     ("3", "2048", ["\x1b[D", "\x1b[A", "\x1b[C"])):
            self.send("g")
            if not self.wait("hub", "М И Н И", timeout=5):
                return
            self.send(number)
            if self.wait(f"game-{number}", title, timeout=5):
                self.send(*moves)
                self.snapshot(f"game-{number}-played")
                if number == "1":                      # Russian layout on: з is the P key
                    self.send("з")
                    self.wait("game-1-paused", "ПАУЗА", timeout=5)
            self.send(ESC)
            self.wait("back-to-dashboard", "мини-игры", timeout=5)
        # The demo asks a question pacman would ask (a package conflict): it must come to us.
        if not self.wait("question", "Нужен твой ответ", timeout=60):
            return
        self.send("n", ENTER)
        # A game left open when the update ends: the strip says so, Esc goes to the summary.
        self.send("g", "1")
        if not self.wait("finished-while-playing", "обновление завершено", timeout=90):
            return
        self.send(ESC)
        if not self.wait("summary", "это была имитация", timeout=60):
            return
        time.sleep(1.2)                                # the summary ignores keys for its first second
        if any(line in self.text() for line in REPORT_LINES):     # the demo never reports, so never says it did
            self.snapshot("summary-FAILED")
            self.failures.append("итог демо говорит об отчёте")
        # S sends this update's report by hand: offered (nothing was queued), and in the demo it only says so
        if "S — отправить отчёт" not in self.text():
            self.snapshot("summary-keys-FAILED")
            self.failures.append("итог: нет клавиши «S — отправить отчёт»")
        if "закроется через" in self.text():
            # «любая клавиша — оставить»: S as that key only stops the countdown, and sends nothing
            self.send("s")
            if not self.gone("summary-kept", "закроется через", timeout=5):
                return
            time.sleep(0.5)
            if DEMO_SAYS in self.text():
                self.snapshot("summary-kept-FAILED")
                self.failures.append("итог: S, что остановила отсчёт, ещё и сработала")
        self.send("s")
        self.wait("summary-demo-key", DEMO_SAYS, timeout=5)
        self.send(ENTER)

    def quit_from_menu(self):
        """The Russian layout is on: й, the Q key, still leaves the menu."""
        if self.wait("menu", "Обновить автоматически", timeout=40):
            self.send("й")

    def manual(self):
        """Обычный Arch-Update, and a password typed ahead while the update gets ready: arch-update's
        pty would echo it onto the screen and into the journal, so it must never get there. Every
        question after that gets Enter (in the demo that goes on whatever the answer)."""
        if not self.wait("menu", "Обычный Arch-Update", timeout=40):
            return
        self.send(DOWN, ENTER, pause=0.5)
        self.send(TYPED_AHEAD)
        if not self.wait("manual", "Proceed with update", timeout=30):
            return
        end = time.time() + 120
        while time.time() < end and self.running() and "это была имитация" not in self.text():
            if TYPED_AHEAD in self.text():
                self.snapshot("manual-typed-ahead-FAILED")
                self.failures.append("ручной режим: набранное заранее попало в arch-update")
                return
            self.send(ENTER, pause=1.5)
        if not self.wait("summary", "это была имитация", timeout=5):
            return
        time.sleep(1.2)                                # the summary ignores keys for its first second
        self.send(ENTER)
        self.after.append(self.typed_ahead_not_logged)

    def typed_ahead_not_logged(self):
        for path in glob.glob(os.path.join(self.home, ".local/state/arch-update-auto/demo-logs/*.log")):
            with open(path, encoding="utf-8", errors="replace") as f:
                if TYPED_AHEAD in f.read():
                    self.failures.append(f"ручной режим: набранное заранее попало в журнал {os.path.basename(path)}")

    def gone(self, stage, needle, timeout=20):
        """Wait until the needle has left the screen."""
        end = time.time() + timeout
        while needle in self.text() and time.time() < end:
            time.sleep(0.1)
        if needle in self.text():
            self.snapshot(stage + "-FAILED")
            self.failures.append(f"{stage}: {needle!r} не исчезло")
            return False
        return True

    def settings(self):
        """Настройки: the update reports go on and off again, "Что именно уходит" shows a report put
        together on the spot. Afterwards the choice is "no" and not a single report was made."""
        if not self.wait("menu", "Обновить автоматически", timeout=40):
            return
        # The news decide whether there is a news item: once they are in, the item list stays put.
        if not self.gone("menu-news", "загружаю новости"):
            return
        self.send(UP, UP)                           # round from the first item to the one before "Выйти"
        if not self.wait("menu-settings", "▸ Настройки", timeout=5):
            return
        if "отчёты об обновлениях: ещё не решено" not in self.text():
            self.failures.append("меню: нет подписи «отчёты об обновлениях: ещё не решено»")
        # The Enter that opens the settings lands on the switch: pressed twice, or held down (the key
        # repeats after about half a second, then every 40 ms), it must still not switch them on.
        self.send(ENTER + ENTER)
        if not self.still_off("settings-double-enter"):
            return
        if "новое: отчёты" in self.text():          # the first start's hint pointed here: done with
            self.failures.append("настройки: подсказка «новое: отчёты…» осталась на экране настроек")
        self.send(ESC)
        if not self.wait("menu-again", "▸ Настройки", timeout=5):
            return
        self.send(ENTER, pause=0.6)
        self.send(*[ENTER] * 12, pause=0.04)
        if not self.still_off("settings-held-enter"):
            return
        self.unpainted("settings")
        time.sleep(1)                               # a fresh press after a pause is a choice
        self.send(ENTER)                            # the switch is the first item
        if not self.wait("settings-on", "ID этой машины", timeout=5):
            return
        if self.switch("Отчёты об обновлениях") != "ВКЛ":
            self.failures.append("настройки: после Enter отчёты не включились")
        if "когда подключат сервер" not in self.text():     # there is no server here: the toast says so
            self.failures.append("настройки: после включения обещана отправка, а сервера нет")
        self.send(DOWN, ENTER)
        if not self.wait("preview", '"device"', timeout=60):
            return
        self.unpainted("preview")
        self.send(ESC)
        if not self.wait("settings-back", "Отчёты об обновлениях", timeout=5):
            return
        self.send(UP, ENTER)
        if not self.wait("settings-off", "ВЫКЛ", timeout=5):
            return
        try:
            with open(os.path.join(self.home, ".local/state/arch-update-auto/settings.json")) as f:
                rid = json.load(f).get("report_id")
        except (OSError, ValueError):
            rid = None
        if not rid or f"ID этой машины в прошлых отчётах: {rid}" not in self.text():
            self.failures.append(f"настройки: выключены, а ID машины ({rid}) не показан")
        # Restarting the services by itself: on with a fresh press, explained, saved; off again.
        self.send(DOWN, DOWN)
        if not self.wait("settings-services", "▸ Перезапускать сервисы автоматически", timeout=5):
            return
        if "dbus" not in self.text() or "клавиша R" not in self.text():
            self.failures.append("настройки: нет объяснения, что перезапускается и что делает R")
        for on in (True, False):
            time.sleep(1)
            self.send(ENTER)
            if not self.wait(f"settings-services-{'on' if on else 'off'}",
                             "перезапускаться сами" if on else "только по клавише R", timeout=5):
                return
            if self.switch("Перезапускать сервисы автоматически") != ("ВКЛ" if on else "ВЫКЛ") or \
                    self.setting("restart_services") is not on:
                self.failures.append(f"настройки: перезапуск сервисов не {'включился' if on else 'выключился'}"
                                     f" (settings.json: {self.setting('restart_services')!r})")
        # The last updates: none here yet.
        self.send(DOWN)
        if not self.wait("settings-updates", "▸ Последние обновления", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("updates-empty", "пока ни одного обновления", timeout=10):
            return
        self.unpainted("updates-empty")
        self.send(ESC)
        if not self.wait("settings-again", "▸ Последние обновления", timeout=5):
            return
        self.send(ESC)
        if not self.wait("menu-back", "отчёты об обновлениях: выкл", timeout=5):
            return
        self.send("q")
        self.after.append(self.nothing_reported)

    def switch(self, title):
        """ВКЛ or ВЫКЛ, as the settings' row of that title shows it ("" without one)."""
        row = next((line for line in self.text().split("\n") if title in line), "")
        return "ВЫКЛ" if "ВЫКЛ" in row else "ВКЛ" if "ВКЛ" in row else ""

    def setting(self, key):
        try:
            with open(os.path.join(self.state, "settings.json")) as f:
                return json.load(f).get(key)
        except (OSError, ValueError) as e:
            return e

    def still_off(self, stage):
        """The settings are open, the reports still off and no choice recorded."""
        if not self.wait(stage, "Отчёты об обновлениях", timeout=5):
            return False
        time.sleep(0.5)
        if self.switch("Отчёты об обновлениях") != "ВЫКЛ" or \
                os.path.exists(os.path.join(self.home, ".local/state/arch-update-auto/settings.json")):
            self.snapshot(stage + "-FAILED")
            self.failures.append(f"{stage}: отчёты включились без отдельного нажатия")
            return False
        return True

    def updates(self):
        """Without --demo: the last updates of this machine (three made up), one's report, one sent by hand
        to a closed local port (the real server for the reports is never reached), one deleted with D, D."""
        if not self.wait("menu", "Проверить ещё раз", timeout=40):
            return
        if not self.gone("menu-news", "загружаю новости"):
            return
        self.send(UP, UP)                           # round from the first item to the one before "Выйти"
        if not self.wait("menu-settings", "▸ Настройки", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("settings", "Отчёты об обновлениях", timeout=5):
            return
        self.send(DOWN, DOWN, DOWN)
        if not self.wait("settings-updates", "▸ Последние обновления", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("updates", "всё чисто", timeout=20):
            return
        self.unpainted("updates")
        screen = self.text()
        for needle in ("нужна проверка", "есть что прочитать", "03.11", "02.11", "01.11"):
            if needle not in screen:
                self.failures.append(f"последние обновления: нет «{needle}»")
        if screen.count("не отправлен") != 3:
            self.failures.append(f"последние обновления: «не отправлен» {screen.count('не отправлен')} раз, а не 3")
        self.send(ENTER)                            # the newest one's report
        if not self.wait("update-report", '"device"', timeout=60):
            return
        if "Отчёт об обновлении 03.11" not in self.text():
            self.failures.append("отчёт: нет заголовка с датой обновления")
        self.unpainted("update-report")
        self.send(ESC)
        if not self.wait("updates-back", "всё чисто", timeout=5):
            return
        self.send(DOWN, DOWN)                       # the oldest: from before the summaries
        if not self.wait("updates-oldest", "▸ 01.11", timeout=5):
            return
        self.send("s")
        if not self.wait("updates-sent", "нет связи с сервером", timeout=30):
            return
        self.send("d")
        if not self.wait("updates-delete", "нажми D ещё раз", timeout=5):
            return
        time.sleep(1)                               # the second D is a press of its own
        self.send("в")                              # D on the Russian layout
        if not self.wait("updates-deleted", "удалено", timeout=10) or not self.gone("updates-gone", "01.11"):
            return
        self.send(ESC)
        if not self.wait("settings-back", "▸ Последние обновления", timeout=5):
            return
        self.send(ESC)
        if not self.wait("menu-back", "Проверить ещё раз", timeout=5):
            return
        self.send("q")
        self.after.append(self.updates_left)

    def updates_left(self):
        logs = os.path.join(self.state, "logs")
        left = sorted(os.listdir(logs)) if os.path.isdir(logs) else []
        gone = [n for n in left if n.startswith(UPDATES[0][0])]
        if gone:
            self.failures.append(f"удалённое обновление осталось: {gone}")
        for stamp, _, summary in UPDATES[1:]:
            for ext in (".log", ".json"):
                if stamp + ext not in left:
                    self.failures.append(f"пропало обновление, которое не удаляли: {stamp}{ext}")
        try:
            with open(os.path.join(self.state, "reports", "manual.json")) as f:
                records = json.load(f)
        except (OSError, ValueError):
            records = {}
        if any(isinstance(r, dict) and r.get("sent") for r in records.values()):
            self.failures.append(f"отчёт записан отправленным, хотя сервера нет: {records}")
        if self.setting("reports") is True:
            self.failures.append("отправка вручную включила отчёты")
        for folder in ("outbox", "sent"):
            path = os.path.join(self.state, "reports", folder)
            if os.path.isdir(path) and os.listdir(path):
                self.failures.append(f"отчёты в reports/{folder}: {sorted(os.listdir(path))}")

    def banner(self):
        """The demo's last update said not to reboot, in this boot: the menu warns about it; F, S and the
        summary opened from the menu (F, R there too) only say that the demo does not do that."""
        if not self.wait("menu", "/boot/initramfs-linux.img", timeout=40):
            return
        if not self.gone("menu-news", "загружаю новости"):
            return
        screen = self.text()
        if "НЕ перезагружайся" not in screen or "F — собрать initramfs заново" not in screen or \
                "S — отправить отчёт" not in screen:
            self.snapshot("menu-banner-FAILED")
            self.failures.append("меню: нет предупреждения о последнем обновлении с клавишами F и S")
        for key in ("f", "ы"):                      # F, and S on the Russian layout
            self.send(key)
            if not self.wait(f"menu-key-{key}", DEMO_SAYS, timeout=5) or not self.gone(f"menu-key-{key}", DEMO_SAYS):
                return
        self.send(UP, UP, UP)                       # round from the first item: above "Настройки"
        if not self.wait("menu-last", "▸ Последнее обновление", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("summary-again", "Нужна проверка", timeout=10):
            return
        time.sleep(1.2)                             # the summary ignores keys for its first second
        screen = self.text()
        for key in ("S — отправить отчёт", "R — перезапустить сервис", "F — "):
            if key not in screen:
                self.failures.append(f"итог из меню: нет «{key}»")
        for key in ("f", "r"):
            self.send(key)
            if not self.wait(f"summary-key-{key}", DEMO_SAYS, timeout=5) or \
                    not self.gone(f"summary-key-{key}", DEMO_SAYS):
                return
        self.send(ENTER)                            # back to the menu, where the warning still is
        if not self.wait("menu-again", "▸ Последнее обновление", timeout=5):
            return
        self.send("q")
        self.after.append(self.bad_untouched)

    def sends(self):
        """Without --demo, with the stand-in for the server for the reports: S under the menu's warning and the summary
        opened before the answer came; then an update's summary, S and Enter at once."""
        if not self.wait("menu", "/boot/initramfs-linux.img", timeout=40):
            return
        if not self.gone("menu-news", "загружаю новости"):
            return
        self.send("s")
        if not self.wait("menu-sending", "отправляю отчёт", timeout=5):
            return
        self.send(DOWN, DOWN)
        if not self.wait("menu-last", "▸ Последнее обновление", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("summary-sending", "Нужна проверка", timeout=10):
            return
        if not self.wait("summary-sent", "отчёт отправлен разработчику (№ 7)", timeout=30):
            return
        time.sleep(0.5)
        if "отправляю отчёт" in self.text():
            self.snapshot("summary-sent-FAILED")
            self.failures.append("итог из меню: «отправляю отчёт…» осталось после ответа сервера")
        time.sleep(1.2)                             # the summary ignores keys for its first second
        self.send(ENTER)
        if not self.wait("menu-again", "▸ Последнее обновление", timeout=10):
            return
        if "S — " in self.text():                  # it went out: not offered again here
            self.failures.append("меню: «S — отправить отчёт» предлагается и после того, как отчёт ушёл")
        self.send(UP, UP)
        if not self.wait("menu-auto", "▸ Обновить автоматически", timeout=5):
            return
        self.send(ENTER)
        if not self.wait("dashboard", "мини-игры", timeout=20):
            return
        if not self.wait("summary", "S — отправить отчёт", timeout=90):
            return
        time.sleep(1.2)
        self.send("s", ENTER)                       # and leave at once: the answer is waited for
        if not self.wait("summary-waits", "дожидаюсь ответа сервера", timeout=5):
            return
        if self.wait("summary-answer", "отчёт отправлен разработчику (№ 8)", timeout=30):
            self.after.append(self.sends_kept)

    def sends_kept(self):
        try:
            with open(os.path.join(self.state, "reports", "manual.json"), encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError) as e:
            records = e
        issues = sorted(r.get("issue") for r in records.values()) if isinstance(records, dict) else records
        if issues != [7, 8]:
            self.failures.append(f"отправленные вручную отчёты не записаны: {records!r}")
        if len(self.relay.got) != 2:
            self.failures.append(f"серверу пришло отчётов: {len(self.relay.got)}, а не 2")
        if self.setting("reports") is True or not self.setting("report_id"):
            self.failures.append(f"settings.json после отправки вручную: reports={self.setting('reports')!r}, "
                                 f"report_id={self.setting('report_id')!r}")

    def keys(self):
        """Without --demo, with the stand-ins: F under the menu's warning and R on the summary of an update go to
        the plain terminal (where sudo only says what it would run) and back with Enter; what came of them is
        said, and kept in the updates' summaries and journals."""
        if not self.wait("menu", "/boot/initramfs-linux.img", timeout=40):
            return
        if not self.gone("menu-news", "загружаю новости"):
            return
        self.send("f")
        if not self.wait("fix-terminal", "Enter — вернуться в Arch-Update", timeout=60):
            return
        if "[sudo в тесте] mkinitcpio -P" not in self.text():
            self.failures.append("F: в терминале не видно, что запускается mkinitcpio -P")
        self.send(ENTER)
        # Whatever the look at this machine's boot files finds now, the menu says it
        if not self.wait("fix-back", "всё ещё нет /boot", "больше не нашёл", timeout=10):
            return
        self.send(ENTER)                            # the first item: «Обновить автоматически»
        if not self.wait("dashboard", "мини-игры", timeout=20):
            return
        if not self.wait("summary", "R — перезапустить сервис", timeout=90):
            return
        time.sleep(1.2)                             # the summary ignores keys for its first second
        self.send("к")                              # R on the Russian layout
        if not self.wait("restart-terminal", "Enter — вернуться в Arch-Update", timeout=30):
            return
        if "[sudo в тесте] systemctl restart -- cups.service" not in self.text():
            self.failures.append("R: в терминале не видно перезапуска cups.service")
        self.send(ENTER)
        if not self.wait("restart-back", "перезапущен сервис: cups", timeout=10):
            return
        if "R — перезапустить" in self.text():
            self.failures.append("итог: R предлагается и после перезапуска")
        time.sleep(1.2)                             # back from the terminal, keys count after a second again
        self.send(ENTER)
        self.after.append(self.keys_kept)

    def keys_kept(self):
        logs = os.path.join(self.state, "logs")
        newest = sorted(n for n in os.listdir(logs) if n.endswith(".json"))[-1]
        with open(os.path.join(logs, newest), encoding="utf-8") as f:
            summary = json.load(f)
        if newest.startswith(self.bad_stamp):
            self.failures.append("обновление не оставило своего итога")
        elif "cups.service" not in (summary.get("services") or {}).get("restarted", []) or \
                any("ждёт перезапуска" in w for w in summary.get("warnings", [])):
            self.failures.append(f"итог обновления не знает о перезапуске: {summary.get('services')}")
        with open(os.path.join(logs, self.bad_stamp + ".json"), encoding="utf-8") as f:
            if "rechecked" not in json.load(f):
                self.failures.append("после F файлы загрузки не проверены заново")
        with open(os.path.join(logs, self.bad_stamp + ".log"), encoding="utf-8") as f:
            journal = f.read()
        if "ИСПРАВЛЕНИЕ] по клавише F" not in journal or "[sudo в тесте]" in journal:
            self.failures.append("журнал исправления: нет события или в нём вывод терминала")

    def bad_untouched(self):
        path = os.path.join(self.state, "demo-logs", self.bad_stamp + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                now = json.load(f)
        except (OSError, ValueError) as e:
            now = e
        if now != self.bad:
            self.failures.append(f"демо изменило итог обновления: {now!r}")
        if os.path.exists(os.path.join(self.state, "reports")):
            self.failures.append(f"демо тронуло отчёты: {sorted(os.listdir(os.path.join(self.state, 'reports')))}")

    def nothing_reported(self):
        state = os.path.join(self.home, ".local/state/arch-update-auto")
        try:
            with open(os.path.join(state, "settings.json")) as f:
                reports = json.load(f).get("reports")
        except (OSError, ValueError) as e:
            reports = e
        if reports is not False:
            self.failures.append(f"settings.json: reports = {reports!r}, а должно быть false")
        for folder in ("outbox", "sent"):
            path = os.path.join(state, "reports", folder)
            if os.path.isdir(path) and os.listdir(path):
                self.failures.append(f"отчёты в reports/{folder}: {sorted(os.listdir(path))}")

    def demo_sent_nothing(self):
        reports = os.path.join(self.home, ".local/state/arch-update-auto/reports")
        found = {d: sorted(os.listdir(os.path.join(reports, d)))
                 for d in ("outbox", "sent", "rejected") if os.path.isdir(os.path.join(reports, d))}
        if found != {"outbox": [EARLIER_REPORT]}:
            self.failures.append(f"демо тронуло отчёты: {found}")
        if os.path.exists(os.path.join(reports, "status.json")):
            self.failures.append("демо пыталось отправить отчёт (появился reports/status.json)")

    def finish(self):
        self.reader.join(20)                            # it ends with the program
        if self.reader.is_alive():
            self.failures.append("не закрылся после итогового экрана")
            try:
                self.child.terminate(force=True)
            except pexpect.ExceptionPexpect:
                pass
            self.reader.join(5)
        try:
            self.child.close()
        except pexpect.ExceptionPexpect:
            pass
        if self.child.exitstatus not in (0, None) or self.child.signalstatus:
            self.failures.append(f"код выхода {self.child.exitstatus}, сигнал {self.child.signalstatus}")
        err = os.path.join(self.home, ".local/state/arch-update-auto/ui-error.log")
        if os.path.exists(err):
            with open(err) as f:
                self.failures.append("ui-error.log:\n" + f.read())
        for check in self.after:
            check()
        shutil.rmtree(self.home, ignore_errors=True)


# Programs running at once. Every one of them redraws its whole animated screen many times a second, all of it
# parsed here (pyte) under one interpreter lock: with more of them this harness's own timing slips (a key "held
# down" would come in later than it was typed), and a run then fails for nothing it tests.
SLOTS = threading.Semaphore(8)


def run(size, results, name=None, script=Run.play, **kw):
    name = name or f"{size[0]}x{size[1]}"
    results[name] = ["не дошёл до конца"]               # replaced below: a run that dies is no pass
    with SLOTS:
        r = None
        try:
            r = Run(*size, name, **kw)
            script(r)
        except Exception as e:                          # a harness problem is a failure too
            if r:
                r.failures.append(f"тест упал: {e!r}")
            else:
                results[name] = [f"тест не запустился: {e!r}"]
        finally:
            if r:
                try:
                    r.finish()
                except Exception as e:
                    r.failures.append(f"тест упал в конце: {e!r}")
                results[name] = r.failures


def main():
    sizes = [tuple(map(int, a.split("x"))) for a in sys.argv[1:]] or SIZES
    results = {}
    threads = [threading.Thread(target=run, args=(s, results), kwargs={"reports_on": s == REPORTS_ON})
               for s in sizes]
    if not sys.argv[1:]:
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-menu-q", Run.quit_from_menu)))
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-manual", Run.manual),
                                        kwargs={"slow_pacman": True}))
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-keys", Run.keys),
                                        kwargs={"demo": False, "update": ONE_UPDATE,
                                                "seed": lambda r: r.seed_bad("logs")}))
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-sends", Run.sends),
                                        kwargs={"demo": False, "update": ONE_UPDATE, "relay": StandInRelay(),
                                                "seed": lambda r: r.seed_bad("logs")}))
        for cols, rows in ((80, 24), (200, 60)):
            threads.append(threading.Thread(target=run, args=((cols, rows), results, f"{cols}x{rows}-settings",
                                                              Run.settings)))
            threads.append(threading.Thread(target=run, args=((cols, rows), results, f"{cols}x{rows}-updates",
                                                              Run.updates),
                                            kwargs={"demo": False, "seed": Run.seed_updates}))
            threads.append(threading.Thread(target=run, args=((cols, rows), results, f"{cols}x{rows}-banner",
                                                              Run.banner), kwargs={"seed": Run.seed_bad}))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bad = 0
    for name in sorted(results, key=lambda n: (tuple(map(int, n.split("-")[0].split("x"))), n)):
        fails = results[name]
        bad += bool(fails)
        print(f"{'✗' if fails else '✓'} {name}")
        for f in fails:
            print("    " + f.replace("\n", "\n    "))
    print(f"снимки экранов: {OUT}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
