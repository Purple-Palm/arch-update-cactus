#!/usr/bin/env python3
"""Headless end-to-end test: drive `arch-update-ui --demo` at laptop and desktop terminal sizes.

Every run gets its own empty HOME (the real one is never touched) and goes through the whole
demo in a real pty: splash, menu, the auto mode dashboard, all three mini-games, the question
modal, a game left open until the update finishes, the summary. Game and menu keys are also
typed with the Russian layout on (з pauses Tetris; a short extra run leaves the menu with й).
The 136x38 run starts with the update reports on, one report from earlier waiting and a server
address where nobody listens: the demo must still queue, send and mention none.
Two more short runs (80x24, 200x60) open the settings with Enter pressed twice and held down (the
reports must stay off), switch them on, look at "Что именно уходит", switch them off again and
check that nothing was queued or sent.
One run (80x24) takes "Обычный Arch-Update" and types a password ahead while the update gets ready
(a stand-in pacman makes that take two seconds): it must reach neither the screen nor the journal.
The output is rendered with pyte; the test fails on a UI exception, a screen that never shows
up, or a bad exit. A text snapshot of every stage is written to tests/out/<cols>x<rows>/.

  python3 tests/run.py              every size, in parallel
  python3 tests/run.py 80x24 ...    only these sizes

Needs python-pexpect and python-pyte (pip: pexpect pyte).
"""
import glob
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


class Run:
    def __init__(self, cols, rows, name=None, reports_on=False, slow_pacman=False):
        self.cols, self.rows = cols, rows
        self.reports_on = reports_on
        self.name = name or f"{cols}x{rows}"
        self.home = tempfile.mkdtemp(prefix=f"arch-update-test-{self.name}-")
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
        if slow_pacman:                 # pacman -Q on a slow machine: the update takes a moment to start
            stub = os.path.join(self.home, "stub")
            os.makedirs(stub)
            with open(os.path.join(stub, "pacman"), "w") as f:
                f.write("#!/bin/sh\nsleep 2\n")
            os.chmod(os.path.join(stub, "pacman"), 0o755)
            env["PATH"] = stub + os.pathsep + env.get("PATH", os.defpath)
        self.child = pexpect.spawn(sys.executable, [UI, "--demo"], env=env, dimensions=(rows, cols))
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
        with socket.socket() as s:                      # a free port, closed again: connections are refused
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.after.append(self.demo_sent_nothing)
        return f"http://127.0.0.1:{port}/report"

    def _drain(self):
        while True:
            try:
                data = self.child.read_nonblocking(65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.EOF, OSError):
                return
            with self.lock:
                self.stream.feed(data)

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
            if not self.child.isalive():
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
        if "отчёт" in self.text():                     # the demo never reports, so never says a word of one
            self.snapshot("summary-FAILED")
            self.failures.append("итог демо говорит об отчёте")
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
        while time.time() < end and self.child.isalive() and "это была имитация" not in self.text():
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
        if "отчёты об обновлениях: не выбраны" not in self.text():
            self.failures.append("меню: нет подписи «отчёты об обновлениях: не выбраны»")
        # The Enter that opens the settings lands on the switch: pressed twice, or held down (the key
        # repeats after about half a second, then every 40 ms), it must still not switch them on.
        self.send(ENTER + ENTER)
        if not self.still_off("settings-double-enter"):
            return
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
        if "ВКЛ" not in self.text() or "ВЫКЛ" in self.text():
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
        self.send(ESC)
        if not self.wait("menu-back", "отчёты об обновлениях: выкл", timeout=5):
            return
        self.send("q")
        self.after.append(self.nothing_reported)

    def still_off(self, stage):
        """The settings are open, the reports still off and no choice recorded."""
        if not self.wait(stage, "Отчёты об обновлениях", timeout=5):
            return False
        time.sleep(0.5)
        if "ВЫКЛ" not in self.text() or \
                os.path.exists(os.path.join(self.home, ".local/state/arch-update-auto/settings.json")):
            self.snapshot(stage + "-FAILED")
            self.failures.append(f"{stage}: отчёты включились без отдельного нажатия")
            return False
        return True

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
        try:
            self.child.expect(pexpect.EOF, timeout=20)
        except pexpect.TIMEOUT:
            self.failures.append("не закрылся после итогового экрана")
            self.child.terminate(force=True)
        self.child.close()
        if self.child.exitstatus not in (0, None) or self.child.signalstatus:
            self.failures.append(f"код выхода {self.child.exitstatus}, сигнал {self.child.signalstatus}")
        err = os.path.join(self.home, ".local/state/arch-update-auto/ui-error.log")
        if os.path.exists(err):
            with open(err) as f:
                self.failures.append("ui-error.log:\n" + f.read())
        for check in self.after:
            check()
        shutil.rmtree(self.home, ignore_errors=True)


def run(size, results, name=None, script=Run.play, reports_on=False, slow_pacman=False):
    r = Run(*size, name, reports_on, slow_pacman)
    try:
        script(r)
    except Exception as e:                              # a harness problem is a failure too
        r.failures.append(f"тест упал: {e!r}")
    finally:
        r.finish()
    results[r.name] = r.failures


def main():
    sizes = [tuple(map(int, a.split("x"))) for a in sys.argv[1:]] or SIZES
    results = {}
    threads = [threading.Thread(target=run, args=(s, results), kwargs={"reports_on": s == REPORTS_ON})
               for s in sizes]
    if not sys.argv[1:]:
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-menu-q", Run.quit_from_menu)))
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-manual", Run.manual),
                                        kwargs={"slow_pacman": True}))
        for cols, rows in ((80, 24), (200, 60)):
            threads.append(threading.Thread(target=run, args=((cols, rows), results, f"{cols}x{rows}-settings",
                                                              Run.settings)))
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
