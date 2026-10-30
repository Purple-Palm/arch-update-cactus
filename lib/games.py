"""Mini-games for the time an update takes (arch-update-ui, by Cactus): Tetris, Snake, 2048.

Every game has the same small interface used by the UI:
  name / title         id for high scores, title shown in the hub
  size()               (width, height) in terminal cells, frame included
  key(k)               a key name from arch-update-ui.parse_keys ("up", "space", "r", ...)
  step(now)            advance the game clock
  draw(c, U, x0, y0, t) draw into the UI Canvas at x0, y0 (U = helpers: grad, lerp, shade, colours)
  score, over, paused
Board cells are two terminal columns wide so they look square.
"""
import random
import time


def frame(c, U, x, y, w, h, color, title=""):
    c.put(x, y, "╭" + "─" * (w - 2) + "╮", color)
    c.put(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", color)
    for yy in range(y + 1, y + h - 1):
        c.put(x, yy, "│", color)
        c.put(x + w - 1, yy, "│", color)
    if title:
        c.put(x + 2, y, f" {title} ", U.TEXT, bold=True)


def block(c, x, y, s, col, ghost=False, alpha=1.0, head=False):
    """One board cell at scale s: 2*s columns by s rows (s=1 keeps the small classic look)."""
    if s == 1:
        c.put(x, y, "░░" if ghost else ("▓▓" if head else "▇▇"), col, alpha=alpha)
        return
    w = 2 * s - 1                       # gap on the right ...
    for r in range(s):
        if ghost:
            c.put(x, y + r, "░" * w, col, alpha=alpha)
        elif r == s - 1:                # ... and half a row at the bottom: every cell reads as a tile
            c.put(x, y + r, "▀" * w, col, alpha=alpha)
        else:
            c.put(x, y + r, ("▓" if head else "█") * w, col, alpha=alpha)


def overlay(c, U, x, y, w, h, lines, t):
    """Centered message over a board (pause / game over)."""
    cy = y + h // 2 - len(lines) // 2
    for i, (text, bold) in enumerate(lines):
        col = U.grad(U.PAL_ARCH, 0.2 - t * 0.15) if bold else U.TEXT
        bx = x + (w - len(text)) // 2
        c.put(bx - 1, cy + i, " " + text + " ", col, bg=U.PANEL, bold=bold)


# ───────────────────────────── Tetris ─────────────────────────────

SHAPES = {
    "I": [(0, 1), (1, 1), (2, 1), (3, 1)],
    "O": [(1, 0), (2, 0), (1, 1), (2, 1)],
    "T": [(1, 0), (0, 1), (1, 1), (2, 1)],
    "S": [(1, 0), (2, 0), (0, 1), (1, 1)],
    "Z": [(0, 0), (1, 0), (1, 1), (2, 1)],
    "J": [(0, 0), (0, 1), (1, 1), (2, 1)],
    "L": [(2, 0), (0, 1), (1, 1), (2, 1)],
}
SHAPE_HUE = {"I": 0.00, "O": 0.14, "T": 0.28, "S": 0.42, "Z": 0.56, "J": 0.70, "L": 0.84}


class Tetris:
    name, title = "tetris", "Тетрис"
    W, H = 10, 20
    scale = 1

    def __init__(self):
        self.reset()

    def reset(self):
        self.board = [[None] * self.W for _ in range(self.H)]
        self.bag = []
        self.next = self._draw_bag()
        self.score = self.lines = 0
        self.level = 1
        self.over = self.paused = False
        self.flash = []          # (row, time) of cleared lines, for a short flash
        self._spawn()
        self.last = time.time()

    def _draw_bag(self):
        if not self.bag:
            self.bag = list(SHAPES)
            random.shuffle(self.bag)
        return self.bag.pop()

    def _spawn(self):
        self.kind, self.next = self.next, self._draw_bag()
        self.cells = list(SHAPES[self.kind])
        self.px, self.py = 3, 0
        if not self._fits(self.cells, self.px, self.py):
            self.over = True

    def _fits(self, cells, px, py):
        for cx, cy in cells:
            x, y = px + cx, py + cy
            if not (0 <= x < self.W and y < self.H) or (y >= 0 and self.board[y][x]):
                return False
        return True

    def _rotate(self):
        if self.kind == "O":
            return
        size = 4 if self.kind == "I" else 3
        rot = [(size - 1 - cy, cx) for cx, cy in self.cells]
        for dx in (0, -1, 1, -2, 2):                      # simple wall kicks
            if self._fits(rot, self.px + dx, self.py):
                self.cells, self.px = rot, self.px + dx
                return

    def _delay(self):
        return max(0.07, 0.8 - (self.level - 1) * 0.07)

    def _lock(self):
        for cx, cy in self.cells:
            if self.py + cy >= 0:
                self.board[self.py + cy][self.px + cx] = self.kind
        full = [y for y in range(self.H) if all(self.board[y])]
        if full:
            now = time.time()
            self.flash = [(y, now) for y in full]
            for y in full:
                del self.board[y]
                self.board.insert(0, [None] * self.W)
            self.lines += len(full)
            self.score += (0, 100, 300, 500, 800)[len(full)] * self.level
            self.level = 1 + self.lines // 10
        self._spawn()

    def _ghost_y(self):
        y = self.py
        while self._fits(self.cells, self.px, y + 1):
            y += 1
        return y

    def size(self):
        return (self.W * 2 * self.scale + 2 + 16, self.H * self.scale + 2)

    def key(self, k):
        if self.over:
            if k == "r":
                self.reset()
            return
        if k == "space" and self.paused:
            self.paused = False
            return
        if k == "p":
            self.paused = not self.paused
        if self.paused:
            return
        if k in ("left", "a") and self._fits(self.cells, self.px - 1, self.py):
            self.px -= 1
        elif k in ("right", "d") and self._fits(self.cells, self.px + 1, self.py):
            self.px += 1
        elif k in ("up", "w", "x"):
            self._rotate()
        elif k in ("down", "s"):
            if self._fits(self.cells, self.px, self.py + 1):
                self.py += 1
                self.score += 1
                self.last = time.time()
        elif k == "space":
            g = self._ghost_y()
            self.score += 2 * (g - self.py)
            self.py = g
            self._lock()
            self.last = time.time()

    def step(self, now):
        if self.over or self.paused:
            self.last = now
            return
        while now - self.last >= self._delay():
            self.last += self._delay()
            if self._fits(self.cells, self.px, self.py + 1):
                self.py += 1
            else:
                self._lock()
                if self.over:
                    return

    def _cell(self, c, U, x, y, kind, t, alpha=1.0, ghost=False, s=None):
        col = U.grad(U.PAL_ARCH, SHAPE_HUE[kind])
        block(c, x, y, self.scale if s is None else s, U.shade(col, 0.55) if ghost else col, ghost=ghost, alpha=alpha)

    def draw(self, c, U, x0, y0, t):
        s = self.scale
        bw = self.W * 2 * s + 2
        bh = self.H * s + 2
        frame(c, U, x0, y0, bw, bh, U.shade(U.grad(U.PAL_ARCH, 0.1), 0.8))
        now = time.time()

        def pos(x, y):
            return x0 + 1 + x * 2 * s, y0 + 1 + y * s
        for y in range(self.H):
            for x in range(self.W):
                k = self.board[y][x]
                px, py = pos(x, y)
                if k:
                    self._cell(c, U, px, py, k, t)
                else:
                    c.put(px + s - 1, py + s // 2, "·", U.FAINT)
        for fy, ft in self.flash:
            a = 1 - (now - ft) / 0.35
            if a > 0:
                for r in range(s):
                    c.put(x0 + 1, y0 + 1 + fy * s + r, "█" * (self.W * 2 * s), (255, 255, 255), alpha=a)
        if not self.over:
            gy = self._ghost_y()
            for cx, cy in self.cells:
                if gy + cy >= 0:
                    self._cell(c, U, *pos(self.px + cx, gy + cy), self.kind, t, ghost=True)
            for cx, cy in self.cells:
                if self.py + cy >= 0:
                    self._cell(c, U, *pos(self.px + cx, self.py + cy), self.kind, t)
        # side panel
        sx = x0 + bw + 3
        c.put(sx, y0 + 1, "СЛЕДУЮЩАЯ", U.DIM)
        for cx, cy in SHAPES[self.next]:
            self._cell(c, U, sx + cx * 2, y0 + 3 + cy, self.next, t, s=1)
        c.put(sx, y0 + 7, "ОЧКИ", U.DIM)
        c.put(sx, y0 + 8, str(self.score), U.TEXT, bold=True)
        c.put(sx, y0 + 10, "ЛИНИИ", U.DIM)
        c.put(sx, y0 + 11, str(self.lines), U.TEXT, bold=True)
        c.put(sx, y0 + 13, "УРОВЕНЬ", U.DIM)
        c.put(sx, y0 + 14, str(self.level), U.TEXT, bold=True)
        if self.paused:
            overlay(c, U, x0, y0, bw, bh, [("ПАУЗА", True), ("P — продолжить", False)], t)
        elif self.over:
            overlay(c, U, x0, y0, bw, bh, [("ИГРА ОКОНЧЕНА", True), (f"{self.score} очков", False),
                                                    ("R — ещё раз", False)], t)

    hints = "←→ двигать   ↑ повернуть   ↓ быстрее   Space сбросить   P пауза"


# ───────────────────────────── Snake ─────────────────────────────

class Snake:
    name, title = "snake", "Змейка"
    W, H = 26, 16
    scale = 1

    def __init__(self):
        self.reset()

    def reset(self):
        cx, cy = self.W // 2, self.H // 2
        self.body = [(cx - i, cy) for i in range(4)]
        self.dir = self.next_dir = (1, 0)
        self.queue = []
        self.score = 0
        self.over = self.paused = False
        self.delay = 0.12
        self.food = self._spawn()
        self.eaten_t = 0.0
        self.last = time.time()

    def _spawn(self):
        free = [(x, y) for x in range(self.W) for y in range(self.H) if (x, y) not in self.body]
        return random.choice(free) if free else None

    def size(self):
        return (self.W * 2 * self.scale + 2, self.H * self.scale + 2)

    def key(self, k):
        if self.over:
            if k == "r":
                self.reset()
            return
        if k in ("space", "p"):
            self.paused = not self.paused
            return
        d = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0),
             "w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}.get(k)
        if d and len(self.queue) < 3:
            last = self.queue[-1] if self.queue else self.dir
            if d != last and d != (-last[0], -last[1]):
                self.queue.append(d)

    def step(self, now):
        if self.over or self.paused:
            self.last = now
            return
        while now - self.last >= self.delay:
            self.last += self.delay
            if self.queue:
                self.dir = self.queue.pop(0)
            hx, hy = self.body[0][0] + self.dir[0], self.body[0][1] + self.dir[1]
            if not (0 <= hx < self.W and 0 <= hy < self.H) or (hx, hy) in self.body[:-1]:
                self.over = True
                return
            self.body.insert(0, (hx, hy))
            if (hx, hy) == self.food:
                self.score += 10
                self.eaten_t = now
                self.delay = max(0.055, self.delay * 0.97)
                self.food = self._spawn()
                if self.food is None:
                    self.over = True
            else:
                self.body.pop()

    def draw(self, c, U, x0, y0, t):
        s = self.scale
        bw, bh = self.W * 2 * s + 2, self.H * s + 2
        frame(c, U, x0, y0, bw, bh, U.shade(U.grad(U.PAL_RUN, 0.1), 0.8))
        for y in range(self.H):
            for x in range(self.W):
                c.put(x0 + 1 + x * 2 * s + s - 1, y0 + 1 + y * s + s // 2, "·", U.FAINT)
        n = len(self.body)
        for i, (x, y) in enumerate(self.body):
            col = U.grad(U.PAL_RUN, i / max(8, n) * 0.6)
            if i == 0:
                col = U.lerp(col, (255, 255, 255), 0.35)
            block(c, x0 + 1 + x * 2 * s, y0 + 1 + y * s, s, col, head=i == 0)
        if self.food:
            pulse = 0.6 + 0.4 * abs(((t * 2.5) % 2) - 1)
            fx, fy = x0 + 1 + self.food[0] * 2 * s, y0 + 1 + self.food[1] * s
            c.put(fx + s - 1, fy + s // 2, "◆", U.lerp(U.BG, U.PINK, pulse), bold=True)
        if self.paused:
            overlay(c, U, x0, y0, bw, bh, [("ПАУЗА", True), ("Space — продолжить", False)], t)
        elif self.over:
            overlay(c, U, x0, y0, bw, bh,
                    [("ИГРА ОКОНЧЕНА", True), (f"{self.score} очков · длина {n}", False), ("R — ещё раз", False)], t)

    hints = "стрелки — поворот   Space пауза"


# ───────────────────────────── 2048 ─────────────────────────────

class G2048:
    name, title = "2048", "2048"
    N, TW, TH = 4, 9, 3     # board size, tile width / height in cells (grown by `scale`)
    scale = 1

    def __setattr__(self, k, v):
        if k == "scale":     # bigger tiles on big screens
            object.__setattr__(self, "TW", 9 + (v - 1) * 7)
            object.__setattr__(self, "TH", 3 + (v - 1) * 2)
        object.__setattr__(self, k, v)

    def __init__(self):
        self.reset()

    def reset(self):
        self.g = [[0] * self.N for _ in range(self.N)]
        self.score = 0
        self.over = self.paused = False
        self.won = False
        self.born = {}            # (x, y) -> time a tile appeared or merged (pop animation)
        self._add()
        self._add()

    def _add(self):
        free = [(x, y) for y in range(self.N) for x in range(self.N) if not self.g[y][x]]
        if free:
            x, y = random.choice(free)
            self.g[y][x] = 4 if random.random() < 0.1 else 2
            self.born[(x, y)] = time.time()

    def _slide(self, row):
        vals = [v for v in row if v]
        out, merged, gain = [], [], 0
        i = 0
        while i < len(vals):
            if i + 1 < len(vals) and vals[i] == vals[i + 1]:
                out.append(vals[i] * 2)
                gain += vals[i] * 2
                merged.append(len(out) - 1)
                i += 2
            else:
                out.append(vals[i])
                i += 1
        return out + [0] * (self.N - len(out)), merged, gain

    def _move(self, d):
        moved, now = False, time.time()
        for i in range(self.N):
            if d in ("left", "right"):
                line = self.g[i][:] if d == "left" else self.g[i][::-1]
            else:
                col = [self.g[y][i] for y in range(self.N)]
                line = col if d == "up" else col[::-1]
            new, merged, gain = self._slide(line)
            self.score += gain
            if d in ("right", "down"):
                new = new[::-1]
                merged = [self.N - 1 - m for m in merged]
            for j, v in enumerate(new):
                x, y = (j, i) if d in ("left", "right") else (i, j)
                if self.g[y][x] != v:
                    moved = True
                self.g[y][x] = v
            for m in merged:
                x, y = (m, i) if d in ("left", "right") else (i, m)
                self.born[(x, y)] = now
                if self.g[y][x] == 2048:
                    self.won = True
        if moved:
            self._add()
            if not self._can_move():
                self.over = True

    def _can_move(self):
        for y in range(self.N):
            for x in range(self.N):
                v = self.g[y][x]
                if not v or (x + 1 < self.N and self.g[y][x + 1] == v) or (y + 1 < self.N and self.g[y + 1][x] == v):
                    return True
        return False

    def size(self):
        return (self.N * (self.TW + 1) + 3, self.N * (self.TH + 1) + 3)

    def key(self, k):
        if self.over:
            if k == "r":
                self.reset()
            return
        d = {"w": "up", "s": "down", "a": "left", "d": "right"}.get(k, k)
        if d in ("up", "down", "left", "right"):
            self._move(d)

    def step(self, now):
        pass

    def draw(self, c, U, x0, y0, t):
        w, h = self.size()
        frame(c, U, x0, y0, w, h, U.shade(U.grad(U.PAL_ARCH, 0.6), 0.8))
        now = time.time()
        for y in range(self.N):
            for x in range(self.N):
                v = self.g[y][x]
                tx, ty = x0 + 2 + x * (self.TW + 1), y0 + 2 + y * (self.TH + 1)
                if not v:
                    for r in range(self.TH):
                        c.fill_bg(tx, ty + r, self.TW, U.PANEL)
                    continue
                lvl = v.bit_length() - 1                     # 2 -> 1 ... 2048 -> 11
                col = U.grad(U.PAL_ARCH, lvl / 11 * 0.85)
                pop = now - self.born.get((x, y), 0)
                if pop < 0.18:
                    col = U.lerp(col, (255, 255, 255), 0.5 * (1 - pop / 0.18))
                bg = U.shade(col, 0.55 + min(0.35, lvl * 0.03))
                for r in range(self.TH):
                    c.fill_bg(tx, ty + r, self.TW, bg)
                s = str(v)
                c.put(tx + (self.TW - len(s)) // 2, ty + self.TH // 2, s, (255, 255, 255), bg=bg, bold=True)
        if self.over:
            overlay(c, U, x0, y0, w, h, [("ХОДОВ НЕТ", True), (f"{self.score} очков", False), ("R — ещё раз", False)], t)
        elif self.won:
            c.put(x0 + 2, y0 + h, "2048 собран! можно играть дальше", U.GOOD)

    hints = "стрелки — сдвигать плитки"


GAMES = [Tetris, Snake, G2048]
