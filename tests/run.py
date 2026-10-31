#!/usr/bin/env python3
"""Headless end-to-end test: drive `arch-update-ui --demo` at laptop and desktop terminal sizes.

Every run gets its own empty HOME (the real one is never touched) and goes through the whole
demo in a real pty: splash, menu, the auto mode dashboard, all three mini-games, the question
modal, a game left open until the update finishes, the summary. Game and menu keys are also
typed with the Russian layout on (з pauses Tetris; a short extra run leaves the menu with й).
The output is rendered with pyte; the test fails on a UI exception, a screen that never shows
up, or a bad exit. A text snapshot of every stage is written to tests/out/<cols>x<rows>/.

  python3 tests/run.py              every size, in parallel
  python3 tests/run.py 80x24 ...    only these sizes

Needs python-pexpect and python-pyte (pip: pexpect pyte).
"""
import os
import shutil
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
DOWN, ESC, ENTER = "\x1b[B", "\x1b", "\r"


class Run:
    def __init__(self, cols, rows, name=None):
        self.cols, self.rows = cols, rows
        self.name = name or f"{cols}x{rows}"
        self.home = tempfile.mkdtemp(prefix=f"arch-update-test-{self.name}-")
        self.dir = os.path.join(OUT, self.name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.shots = 0
        self.failures = []
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "ARCH_UPDATE"))}
        env.update(HOME=self.home, TERM="xterm-256color", LANG="C.UTF-8", LC_ALL="C.UTF-8")
        self.child = pexpect.spawn(sys.executable, [UI, "--demo"], env=env, dimensions=(rows, cols))
        # The UI blocks once the pty buffer is full, so somebody must read all the time.
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()

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
        self.send(ENTER)

    def quit_from_menu(self):
        """The Russian layout is on: й, the Q key, still leaves the menu."""
        if self.wait("menu", "Обновить автоматически", timeout=40):
            self.send("й")

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
        shutil.rmtree(self.home, ignore_errors=True)


def run(size, results, name=None, script=Run.play):
    r = Run(*size, name)
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
    threads = [threading.Thread(target=run, args=(s, results)) for s in sizes]
    if not sys.argv[1:]:
        threads.append(threading.Thread(target=run, args=((80, 24), results, "80x24-menu-q", Run.quit_from_menu)))
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
