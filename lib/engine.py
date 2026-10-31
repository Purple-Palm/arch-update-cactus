"""Engine shared by arch-update-auto (plain terminal) and arch-update-ui (full-screen UI).

Runs the real arch-update in a pty and answers its routine questions (RULES).
Risky or unknown questions are handed to the user through the active View.
Everything is logged per run; after the run the system is checked (/boot,
initramfs, DKMS, Secure Boot signature) and a structured result is returned.
The checks find out what this machine has (kernels, mkinitcpio presets, DKMS,
sbctl) and skip what it does not have: the same code runs on every machine.
"""
import datetime
import glob
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import traceback

import pexpect

ENTER, YES, NO = "\r", "y\r", "n\r"
HANDOVER, NEWS = "handover", "news"

# Answer table: matched against the line the program is waiting on
# (colour-stripped, cursor still on that line). First match wins.
RULES = [
    # arch-update
    ("another arch-update is running", r"Proceed anyway\? \[y/N\]$", NO, None),
    ("proceed with update", r"Proceed with update\? \[Y/n\]$", YES, None),
    ("Arch news", r"Select the news to read .*to proceed with update:$", NEWS, None),
    ("press enter to continue", r'Press "enter" to continue$', ENTER, None),
    ("press enter to quit", r'Press "enter" to quit$', ENTER, None),
    ("remove orphan packages", r"remove (?:this|these) orphan packages?.*\[y/N\]$", NO, "orphans"),
    ("clean package cache", r"remove (?:it|them) from the cache now\? \[Y/n\]$", YES, None),
    ("process pacnew files", r"process (?:this file|these files) now\? \[Y/n\]$", NO, "pacnew"),
    ("reboot now", r"Would you like to reboot now\? \[y/N\]$", NO, "reboot"),
    ("restart services", r"Select the service\(s\) to restart .*without restarting the service\(s\):$", ENTER, "services"),
    ("remove unused flatpaks", r"Flatpak unused packages? now\? \[y/N\]$", NO, None),
    # pacman: things a human should decide
    ("конфликт пакетов", r"are in conflict(?: \(.*\))?\. Remove \S+\? \[y/N\]$", HANDOVER, None),
    ("pacman хочет тронуть важный системный пакет (HoldPkg)", r"HoldPkg was found in target list\. Do you want to continue\? \[y/N\]$", HANDOVER, None),
    ("пакет из списка игнорируемых", r"is in IgnorePkg/IgnoreGroup\. Install anyway\? \[[YyNn]/[YyNn]\]$", HANDOVER, None),
    ("pacman предлагает пропустить пакеты", r"skip the above packages? for this upgrade\? \[[YyNn]/[YyNn]\]$", HANDOVER, None),
    ("скачанный пакет повреждён", r"Do you want to delete it\? \[Y/n\]$", HANDOVER, None),
    ("pacman хочет удалить пакеты", r"Do you want to remove these packages\? \[Y/n\]$", HANDOVER, None),
    ("запрос пароля", r"(?:\[sudo\] password for \S+|[Pp]assword[^:\n]*|[Pp]assphrase[^:\n]*):$", HANDOVER, None),
    # pacman: routine
    ("pacman proceed", r"Proceed with (?:installation|download)\? \[Y/n\]$", YES, None),
    ("flatpak proceed", r"Proceed with these changes to the [^?]+\? \[Y/n\]$", YES, None),
    ("replace package", r"Replace \S+ with \S+\? \[Y/n\]$", YES, None),
    ("import PGP key", r"Import(?: PGP key .*)?\? \[Y/n\]$", YES, None),
    ("choose provider", r"Enter a number \(default=\d+\):?$", ENTER, None),
    ("choose group members", r"Enter a selection \(default=all\):?$", ENTER, None),
    # yay
    ("yay proceed", r"Proceed with install\? \[Y/n\]$", YES, None),
    ("yay remove make deps", r"Remove make dependencies after install\? \[y/N\]$", YES, None),
    # paru: no review, the same trust in AUR updates as yay's "Diffs to show?" -> default
    ("paru review", r"Proceed to review\? \[Y/n\]$", YES, None),
    ("paru accept changes", r"Accept changes\? \[Y/n\]$", YES, None),
]
# paru and flatpak end their questions with ':' ("[Y/n]: "), pacman and yay don't
RULES = [(label, re.compile(rx[:-1] + ":?$" if rx.endswith(r"\]$") else rx), action, note)
         for label, rx, action, note in RULES]

QUIT_PROMPT = re.compile(r'Press "enter" to quit$')
# yay menus print the question above and wait on a bare "==>" line.
YAY_MENU = re.compile(r"Packages to cleanBuild\?|Diffs to show\?|PKGBUILDs to edit\?|Packages to exclude"
                      r"|Enter a number \(default=\d+\)")
# Something that looks like a question nobody taught us about.
UNKNOWN_PROMPT = re.compile(
    r"(?:\[[^\]\n]*[/-][^\]\n]*\]|\?|==>|\((?:[YyNn]|yes|no)(?:/[^)]*)?\)):?$"
    r"|(?i:(?:enter|select|choose|password|passphrase|user ?name|login|answer|input)[^\n]*:)$"
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
FAILURE_MARKERS = re.compile(r"Initcpio image generation FAILED|error: command failed to execute correctly"
                             r"|An error has occurred during the update process"
                             r"|invalid or corrupted package \(PGP signature\)|is unknown trust|is marginal trust"
                             r"|You can do this later by running: #? ?dd if=\S+ of=\S+", re.I)
KERNEL_FLASH = re.compile(r"dd if=\S+ of=\S+")   # depthcharge kernels (ARM Chromebooks): flash by hand
KEYRING_TROUBLE = re.compile(r"PGP signature|unknown trust|marginal trust", re.I)
PASSWORD_LINE = re.compile(r"(?i)(?:pass(?:word|phrase)|пароль)[^\n]{0,120}:[^\S\n]*$")

SETTLE = 0.3        # seconds of silence before we treat the current line as a question
UNKNOWN_IDLE = 12   # seconds of silence on a question-looking line before handing over
STALL_IDLE = 120    # seconds of silence on any prompt-ish line (ends in : ? ] >) before handing over anyway
KEEP_LOGS = 30
# arch-update's exit codes for a run that stopped before it touched anything (list_packages.sh, full_upgrade.sh)
NOTHING_DONE = {4: "обновление отменено — система не менялась",
                17: "уже идёт другое обновление Arch-Update — не перезагружайся, пока оно не закончится; "
                    "это окно ничего не меняло"}
PACMAN_LOCK = "/var/lib/pacman/db.lck"
PACMAN_LOG = "/var/log/pacman.log"
MODULES_DIR = "/usr/lib/modules"          # one directory per installed kernel
PRESET_DIR = "/etc/mkinitcpio.d"
DKMS_SRC = "/usr/src"
POWER_DIR = "/sys/class/power_supply"
STATE_DIR = os.path.expanduser("~/.local/state/arch-update-auto")
REBOOT_PKGS = re.compile(
    r"^(linux(?:-lts|-zen|-hardened)?|linux-firmware.*|nvidia.*|lib32-nvidia-utils|opencl-nvidia|"
    r"amd-ucode|intel-ucode|systemd|glibc|mesa|lib32-mesa|plasma-workspace|kwin|qt6-base|wayland|"
    r"gnome-shell|mutter|gdm|xorg-server|xfwm4|xfce4-session|sway|hyprland|wlroots\S*|sof-firmware|alsa-ucm-conf)$"
)
PKG_ARCH = r"(?:x86_64(?:_v[234])?|aarch64|armv7h|i686|pentium4|riscv64|any)"   # end of a package file name
NVIDIA_PKGS = re.compile(r"^(?:lib32-)?nvidia\S*$")
C_DIM, C_YEL, C_GRN, C_RED, C_BOLD, C_OFF = "\033[2m", "\033[33m", "\033[32m", "\033[31m", "\033[1m", "\033[0m"

TERM_OK = True    # the terminal window is still there
LOG = None
RESPONDER = None  # responder of the running update (for the UI)
RUNNING = False   # arch-update is running right now


# ───────────────────────────── helpers ─────────────────────────────

def notify(title, body, urgent=False):
    if os.environ.get("ARCH_UPDATE_AUTO_NOTIFY", "1") == "0":
        return
    args = ["notify-send", "-a", "Arch-Update", "-i", "arch-update-blue", title, body]
    if urgent:
        args[1:1] = ["-u", "critical"]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def run(cmd, timeout=60, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def pacman_busy():
    return os.path.exists(PACMAN_LOCK) and bool(run(["pgrep", "-x", "pacman"]).strip())


def kernel_packages():
    """{package: kernel version} of the installed kernels (linux, linux-lts, linux-aarch64 …): the
    packages owning a /usr/lib/modules/<version>/modules.builtin. A copy nobody owns (the running
    kernel kept by kernel-modules-hook after an upgrade) doesn't count."""
    files = sorted(glob.glob(os.path.join(MODULES_DIR, "*", "modules.builtin")))
    kernels = {}
    if files:
        # "/usr/lib/modules/7.2.11-arch1-1/modules.builtin is owned by linux 7.2.11.arch1-1"
        for line in run(["pacman", "-Qo"] + files, env=child_env()).splitlines():
            m = re.match(r"^(\S+) is owned by (\S+) \S+$", line)
            if m:
                kernels[m.group(2)] = os.path.basename(os.path.dirname(m.group(1)))
    return kernels


def preset_files(pkg):
    """Boot files mkinitcpio makes for kernel package `pkg` from its preset:
    [("kernel" | "image" | "uki", path)]. Empty when the machine boots some other way
    (booster, dracut, depthcharge kernel partitions): then there is nothing we can check."""
    preset = os.path.join(PRESET_DIR, f"{pkg}.preset")
    if not os.path.exists(preset):
        return []
    out = run(["bash", "-c", 'source "$1" >/dev/null 2>&1 || exit; for p in "${PRESETS[@]}"; do '
                             'k=${p}_kver i=${p}_image u=${p}_uki; echo "kernel ${!k:-$ALL_kver}"; '
                             'echo "image ${!i}"; echo "uki ${!u}"; done', "preset", preset], timeout=10)
    files = []
    for line in out.splitlines():
        kind, _, path = line.partition(" ")
        if path.startswith("/") and (kind, path) not in files:
            files.append((kind, path))
    return files


def boot_file_sizes_as_root(files):
    """Sizes of boot files the user may not look at (/boot closed to users, as archinstall mounts
    the ESP: fmask/dmask 0077), through sudo if it asks for no password: {path: size}, missing
    files left out. None when sudo would ask."""
    out = run(["sudo", "-n", "sh", "-c", 'echo sudo-ok; for f; do stat -c "%s %n" -- "$f" 2>/dev/null; done',
               "sh"] + files, timeout=20)
    lines = out.splitlines()
    if lines[:1] != ["sudo-ok"]:
        return None
    sizes = {}
    for line in lines[1:]:
        size, _, path = line.partition(" ")
        if size.isdigit():
            sizes[path] = int(size)
    return sizes


def dkms_built():
    """{module: {kernel versions it is installed for}} from `dkms status`. A module whose build
    failed shows up as only "added" or "built": it gets an empty or partial set. {} without DKMS."""
    built = {}
    for line in run(["dkms", "status"]).splitlines():
        left, _, state = line.rpartition(":")
        parts = [p.strip() for p in left.split(",")]
        if "/" in parts[0]:                     # "nvidia/615.78, 7.2.11-arch1-1, x86_64: installed", "nvidia/615.78: added"
            module, kver = parts[0].split("/")[0], parts[1] if len(parts) > 1 else None
        else:                                   # older dkms: "nvidia, 615.78, 7.2.11-arch1-1, x86_64: installed"
            module, kver = parts[0], parts[2] if len(parts) > 2 else None
        if not re.fullmatch(r"[\w.+-]+", module):
            continue                            # a warning line, not a module
        mods = built.setdefault(module, set())
        if kver and state.strip().startswith("installed"):
            mods.add(kver)
    return built


def dkms_skips(module, kver):
    """The module's dkms.conf limits it to other kernels (BUILD_EXCLUSIVE_KERNEL): not missing there."""
    for conf in glob.glob(os.path.join(DKMS_SRC, f"{module}-*", "dkms.conf")):
        try:
            with open(conf) as f:
                m = re.search(r'^\s*BUILD_EXCLUSIVE_KERNEL\s*=\s*"?([^"\n]+)"?', f.read(), re.M)
        except OSError:
            continue
        if m:
            try:
                return not re.search(m.group(1), kver)
            except re.error:
                return False
    return False


def installed_versions():
    return dict(line.split(" ", 1) for line in run(["pacman", "-Q"]).splitlines() if " " in line)


def last_lines(text, n):
    return [line.rstrip() for line in text.rstrip().split("\n")[-n:]]


def strip_ansi(text):
    return ANSI.sub("", text)


def child_env():
    # English prompts for RULES, "10.38 MiB" (not "10,38") for the progress parser whatever the
    # machine's locale (LC_ALL would override both, so it becomes LANG); GIT_TERMINAL_PROMPT=0: a
    # vanished AUR git source fails instead of asking; PARU_PAGER: paru prints instead of opening less
    env = dict(os.environ, LANGUAGE="en", LC_MESSAGES="C.UTF-8", LC_NUMERIC="C.UTF-8", GIT_TERMINAL_PROMPT="0",
               PARU_PAGER="cat")
    lc_all = env.pop("LC_ALL", None)
    if lc_all:
        env.setdefault("LANG", lc_all)
    return env


def elevation_command():
    """What arch-update elevates with (its PrivilegeElevationCommand, sudo by default)."""
    conf = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "arch-update", "arch-update.conf")
    try:
        with open(conf) as f:
            for line in f:
                m = re.match(r"\s*PrivilegeElevationCommand\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "sudo"


def sudo_asks_password():
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


def battery():
    """(percent, discharging) of the machine's own battery (the emptiest one), None on a desktop.
    The battery of a wireless mouse or gamepad (scope "Device") doesn't count."""
    found = []
    for d in glob.glob(os.path.join(POWER_DIR, "*")):
        try:
            with open(os.path.join(d, "type")) as f:
                if f.read().strip() != "Battery":
                    continue
            if os.path.exists(os.path.join(d, "scope")):
                with open(os.path.join(d, "scope")) as f:
                    if f.read().strip() == "Device":
                        continue
            with open(os.path.join(d, "capacity")) as f:
                cap = int(f.read().strip())
            with open(os.path.join(d, "status")) as f:
                found.append((cap, f.read().strip() == "Discharging"))
        except (OSError, ValueError):
            continue
    return min(found) if found else None


INHIBIT = ["systemd-inhibit", "--what=sleep:idle", "--mode=block"]   # not shutdown: the user may reboot


def can_inhibit():
    try:
        return subprocess.run(INHIBIT + ["true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_chromebook():
    try:
        with open("/sys/class/dmi/id/sys_vendor") as f:
            return f.read().strip() == "Google"
    except OSError:
        return False


def secure_boot_on():
    try:
        with open("/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", "rb") as f:
            return f.read()[-1:] == b"\x01"
    except OSError:
        return False


def unmounted_boot():
    """Boot partitions from fstab (/boot, /efi, /boot/efi) that are not mounted: pacman would
    write the new kernel into an empty directory and the machine would boot the old one."""
    missing = []
    for target in run(["findmnt", "--fstab", "-n", "-o", "TARGET"]).split():
        if target in ("/boot", "/efi", "/boot/efi"):
            try:
                os.listdir(target)          # wakes up an automount
            except OSError:
                pass
            if not os.path.ismount(target):
                missing.append(target)
    return missing


def failed_units():
    return [l.split()[0] for l in run(["systemctl", "--failed", "--plain", "--no-legend"]).splitlines() if l.strip()]


def check_updates(timeout=90):
    """Run `arch-update -l`. Returns (repo, aur, failed) with lists of (name, old, new)."""
    try:
        r = subprocess.run(["timeout", str(timeout), "arch-update", "-l"], capture_output=True,
                           text=True, env=child_env())
        text, rc = strip_ansi(r.stdout + r.stderr), r.returncode
    except OSError:
        return [], [], True
    repo, aur, other = [], [], []
    section = repo
    for line in text.splitlines():
        if "==> AUR Packages" in line:
            section = aur
        elif "==> Flatpak Packages" in line:
            section = other
        elif "==> Packages" in line:
            section = repo
        m = re.match(r"^\s*(\S+)\s+(\S+)\s+->\s+(\S+)", line)
        if m:
            section.append(m.groups())
    # arch-update -l exits 0 with updates and 7 without; anything else (127: not installed) is a failure
    failed = rc not in (0, 7) or bool(re.search(r"Cannot fetch updates|Unable to retrieve|timed out", text))
    return repo, aur, failed


def split_escape(text):
    """Split off an escape sequence that is cut at the end of a read, so it can be completed by
    the next read. OSC sequences (e.g. sudo's ~220-byte OSC 3008 context markers) end with ST
    (ESC \\): the ESC of an ST belongs to the sequence before it and must not be cut off."""
    osc = text.rfind("\x1b]")
    if osc != -1 and not re.search(r"\x07|\x1b\\", text[osc:]) and len(text) - osc < 4096:
        return text[:osc], text[osc:]                 # unterminated OSC: carry it whole
    esc = text.rfind("\x1b")
    if esc == -1 or text.startswith("\x1b\\", esc) or ANSI.match(text, esc) or len(text) - esc >= 32:
        return text, ""
    return text[:esc], text[esc:]


# ───────────────────────────── logging ─────────────────────────────

class RunLog:
    """Per-run log files: readable .log (CR-aware, colour-stripped, with events) and raw .raw."""
    TOTAL_SNAPSHOT = re.compile(r"^\s*Total \(\s*\d+/\d+\)")

    def __init__(self):
        self.dir = os.environ.get("ARCH_UPDATE_AUTO_LOGDIR", os.path.join(STATE_DIR, "logs"))
        os.makedirs(self.dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = os.path.join(self.dir, stamp + ".log")
        self.txt = open(self.path, "w", buffering=1, encoding="utf-8")
        self.raw = open(os.path.join(self.dir, stamp + ".raw"), "wb", buffering=0)
        self.line, self.carry, self.cr = "", "", False
        self.broken = False   # a write failed (disk full, quota, I/O error): logging stopped
        latest = os.path.join(self.dir, "latest.log")
        try:
            if os.path.islink(latest) or os.path.exists(latest):
                os.remove(latest)
            os.symlink(os.path.basename(self.path), latest)
        except OSError:
            pass
        for old in sorted(glob.glob(os.path.join(self.dir, "20*.log")))[:-KEEP_LOGS]:
            for f in (old, old[:-4] + ".raw"):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # The log must never stop the update: a failed write only switches logging off.
    def output(self, data):
        self._safe(self._output, data)

    def event(self, kind, msg):
        self._safe(self._event, kind, msg)

    def text(self, msg):
        self._safe(self._text, msg)

    def _safe(self, fn, *args):
        if self.broken:
            return
        try:
            fn(*args)
        except (OSError, ValueError):
            self.broken = True

    def _output(self, data):
        self.raw.write(data)
        text = self.carry + data.decode("utf-8", "replace")
        text, self.carry = split_escape(text)   # keep an escape split across reads for the next chunk
        for ch in ANSI.sub("", text):
            if ch == "\n":
                if not (self.TOTAL_SNAPSHOT.match(self.line) and not self.line.rstrip().endswith("100%")):
                    self.txt.write(self.line + "\n")
                self.line, self.cr = "", False
            elif ch == "\r":
                self.cr = True      # a redraw ends with '\r': drop the line only once new text follows,
                                    # so pacman's "100%\r" + "\r\n" (or a split read) keeps it
            elif ch == "\x08":
                self.line = self.line[:-1]      # an echoed Backspace ("\b \b") takes back what it erased
            elif ch != "\x07":
                if self.cr:
                    self.line, self.cr = "", False
                self.line += ch

    def _event(self, kind, msg):
        if self.line:
            self.txt.write(self.line + "\n")
        self.line, self.cr = "", False
        self.txt.write(f"[{datetime.datetime.now():%H:%M:%S} {kind}] {msg}\n")

    def _text(self, msg):
        if self.line:                       # our own messages start on a fresh line
            self.txt.write(self.line + "\n")
            self.line, self.cr = "", False
        self.txt.write(ANSI.sub("", msg))


# ───────────────────────────── history / progress ─────────────────────────────

class History:
    """Durations of past runs, used to estimate how long the next one takes."""
    path = os.path.join(STATE_DIR, "history.json")
    defaults = {"sec_per_pkg": 1.2, "hooks_sec": 25.0, "dl_bps": 8e6, "aur": {}, "aur_default": 90.0,
                "cleanup_sec": 6.0}

    def __init__(self):
        self.d = json.loads(json.dumps(self.defaults))   # deep copy: never mutate the defaults
        try:
            with open(self.path) as f:
                self.d.update(json.load(f))
        except (OSError, ValueError):
            pass

    def ema(self, key, value, w=0.4):
        if value and value > 0:
            self.d[key] = (1 - w) * self.d.get(key, value) + w * value

    def aur_time(self, pkg):
        return self.d["aur"].get(pkg, self.d["aur_default"])

    def save(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.d, f, indent=1)
        except OSError:
            pass


UNIT = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


class Progress:
    """Turns pacman / yay / arch-update output lines into stages, percentages and an ETA."""
    STAGES = ["download", "install", "hooks", "aur", "cleanup"]

    def __init__(self, repo_count, aur_pkgs, history):
        self.h = history
        self.repo_count = repo_count
        self.aur_pkgs = [p for p, *_ in aur_pkgs]
        self.stage = "start"
        self.stage_t = time.time()
        self.t0 = time.time()
        self.total = repo_count or 0
        self.done = 0
        self.dl_pct = 0.0
        self.dl_bytes = 0
        self.dl_seen = False
        self.hooks_total = 0
        self.hooks_done = 0
        self.hook_name = ""
        self.current = ""
        self.building = None
        self.build_t = 0.0
        self.aur_done = 0
        self.timings = {}
        self.in_repo = True
        self.seen_dl = set()
        self.aur_total = 0        # AUR packages yay will actually build (incl. new AUR dependencies)
        self.aur_learned = {}     # build times of this run, folded into history only by learn()

    def _go(self, stage):
        now = time.time()
        self.timings.setdefault(self.stage, 0.0)
        self.timings[self.stage] += now - self.stage_t
        self.stage, self.stage_t = stage, now

    PHRASES = [
        (r"^:: Synchronizing package databases", "синхронизирую базы пакетов"),
        (r"^:: Starting full system upgrade", "начинаю обновление системы"),
        (r"^resolving dependencies", "проверяю зависимости"),
        (r"^looking for conflicting packages", "ищу конфликты"),
        (r"^(==> )?Looking for orphan packages", "ищу пакеты-сироты"),
        (r"^(==> )?No orphan package", "сирот нет"),
        (r"^(==> )?Looking for old cached packages", "проверяю кэш пакетов"),
        (r"^(==> )?No old or uninstalled cached package", "кэш в порядке"),
        (r"^(==> )?Looking for pacnew", "ищу .pacnew файлы"),
        (r"^(==> )?No pacnew/pacsave file", ".pacnew файлов нет"),
        (r"^(==> )?The update has been applied", "обновление применено"),
        (r"^(==> )?Looking for updates", "ищу обновления"),
        (r"^:: Searching AUR for updates", "ищу обновления в AUR"),
        (r"^==> (Checking runtime dependencies|Checking buildtime dependencies)", "проверяю зависимости сборки"),
        (r"^==> Retrieving sources", "скачиваю исходники"),
        (r"^==> Validating source files", "проверяю контрольные суммы"),
        (r"^==> (Extracting sources|Starting (build|package|prepare)\(\))", None),
        (r"^==> Creating package", "упаковываю"),
        (r"^loading packages", "загружаю пакеты"),
        (r"^:: Running pre-transaction hooks", "подготовка"),
    ]

    # arch-update 4.4.3's own messages after the package steps (update.sh, orphan_packages.sh,
    # packages_cache.sh, pacnew_files.sh, kernel_reboot.sh, restart_services.sh)
    CLEANUP = re.compile(r"^==> (The update has been applied|No orphan package|Orphan Packages:|Looking for orphan|"
                         r"No old or uninstalled cached|Cached Packages:|Looking for old cached|No pacnew|"
                         r"Pacnew Files:|Looking for pacnew|No pending kernel update|Reboot required:|"
                         r"No service requiring|Services:)")

    def aur_count(self):
        return max(len(self.aur_pkgs), self.aur_total, self.aur_done + (1 if self.building else 0)) or 1

    def feed_line(self, line):
        """Returns a short human line for the activity feed, or None."""
        s = line.strip()
        if not s or re.search(r"\[[YyNn]/[YyNn]\]|^==>\s*$|^==> \[N\]one|Packages to (exclude|cleanBuild)|"
                              r"Diffs to show|is up to date$|^(Total Installed|Net Upgrade) Size|"
                              r"^(==> )?(Packages|AUR Packages|Flatpak Packages|Arch News):$|Press \"enter\" to", s):
            return None     # prompt echoes and noise: the answers show up as their own feed items
        if s.startswith("==> Updating Packages"):
            self.in_repo = True
            return "обновляю пакеты из репозиториев"
        if s.startswith("==> Updating AUR Packages"):
            self.in_repo = False
            self._go("aur")
            return "обновляю пакеты из AUR"
        m = re.match(r"^Packages \((\d+)\)", s)
        if m:
            if self.in_repo:
                self.total = int(m.group(1))
            return None
        m = re.match(r"^Total Download Size:\s+([\d.]+)\s+(\w+)", s)
        if m:
            if self.in_repo:    # yay's repo-dependency installs in the AUR step must not overwrite it
                self.dl_bytes = float(m.group(1)) * UNIT.get(m.group(2), 1)
            return f"скачать {m.group(1)} {m.group(2)}"
        m = re.match(r"^AUR (?:Explicit|Dependency|Make Dependency|Check Dependency) \((\d+)\):", s)
        if m:
            self.aur_total += int(m.group(1))
            return None
        m = re.match(rf"^(\S+)-[^-\s]+-[^-\s]+-{PKG_ARCH}$", s)
        if m and self.stage == "download":       # pacman 7 prints one such line when a file starts
            name = m.group(1)
            if name not in self.seen_dl:
                self.seen_dl.add(name)
                return "качаю " + name
            return None
        if s.startswith(":: Retrieving packages"):
            self._go("download") if self.in_repo else None
            return "скачиваю пакеты"
        m = re.search(r"Total \(\s*(\d+)/(\d+)\).*?(\d+)%\s*$", s)
        if m:
            self.dl_seen = True
            self.dl_pct = float(m.group(3))
            return None
        m = re.match(r"^\S+\s+[\d.]+ \w+\s+[\d.]+ \w+/s\s+\S+\s+\[[#\-o ]*\]\s+100%$", s)
        if m:
            name = s.split()[0]
            if name in self.seen_dl or name == "Total":
                return None
            self.seen_dl.add(name)
            return "скачан " + re.sub(rf"-[^-]+-[^-]+-{PKG_ARCH}$", "", name)
        if s.startswith(":: Running pre-transaction hooks"):
            return "подготовка"
        if s.startswith(":: Processing package changes"):
            if self.in_repo:
                self._go("install")
            return None
        m = re.match(r"^\(\s*(\d+)/(\d+)\) (upgrading|installing|reinstalling|removing|downgrading) (\S+)", s)
        if m:
            verb = {"upgrading": "обновляю", "installing": "устанавливаю", "reinstalling": "переустанавливаю",
                    "removing": "удаляю", "downgrading": "откатываю"}[m.group(3)]
            if self.in_repo:
                if self.stage != "install":
                    self._go("install")
                self.done, self.total = int(m.group(1)), int(m.group(2))
            self.current = m.group(4)
            return f"{verb} {m.group(4)}"
        if s.startswith(":: Running post-transaction hooks"):
            if self.in_repo:
                self._go("hooks")
            self.hooks_done = 0
            return "выполняю хуки"
        if self.stage == "hooks" or (not self.in_repo and s[:1] == "("):
            m = re.match(r"^\(\s*(\d+)/(\d+)\) (.+)$", s)
            if m and not re.search(r"\[[#\-]*\]\s+\d+%$", s):     # not a "checking keys [###] 100%" bar
                self.hooks_done, self.hooks_total, self.hook_name = int(m.group(1)), int(m.group(2)), m.group(3)
                return m.group(3).rstrip(".")
        m = re.match(r"^==> Making package: (\S+)", s)
        if m:
            if self.stage != "aur":
                self._go("aur")
            self.building, self.build_t = m.group(1), time.time()
            return f"собираю {m.group(1)}"
        m = re.match(r"^==> Finished making: (\S+)", s)
        if m:
            if self.building:
                self.aur_learned[self.building] = round(time.time() - self.build_t, 1)
            self.building = None
            self.aur_done += 1
            return f"собран {m.group(1)}"
        m = re.match(r"^:: (\d+) packages? to upgrade/install", s)
        if m:
            return f"в AUR к обновлению: {m.group(1)}"
        m = re.match(r"^:: \(\d+/\d+\) Downloaded PKGBUILD: (\S+)", s)
        if m:
            return f"получен PKGBUILD {m.group(1)}"
        phrase = next((ru for rx, ru in self.PHRASES if re.match(rx, s)), "")
        if self.CLEANUP.match(s):
            # arch-update checks orphans right before the AUR step too: only a check that comes after
            # all package steps (or the final "applied" line) starts the cleanup stage
            after_pkgs = s.startswith("==> The update has been applied") or not self.in_repo
            if self.stage not in ("cleanup", "start") and after_pkgs:
                self._go("cleanup")
            return phrase or s[4:]
        if phrase is None or phrase:
            return phrase
        if s.startswith("==> ") or s.startswith(":: "):
            return s[4:] if s.startswith("==> ") else s[3:]
        if re.match(r"^(error|warning):", s):
            return s
        if re.match(r"^(?:sudo|doas|su): |^Sorry, try again|.*account is locked|.*left to unlock", s):
            return s
        return None

    def feed_partial(self, s):
        """The line pacman keeps redrawing in place (download total)."""
        m = re.search(r"Total \(\s*(\d+)/(\d+)\).*?(\d+)%\s*$", s.strip())
        if m:
            self.dl_seen = True
            self.dl_pct = float(m.group(3))

    def stage_progress(self):
        if self.stage == "download":
            return min(1.0, self.dl_pct / 100) if self.dl_seen else 0.0
        if self.stage == "install":
            return self.done / self.total if self.total else 0.0
        if self.stage == "hooks":
            return self.hooks_done / self.hooks_total if self.hooks_total else 0.0
        if self.stage == "aur":
            n = self.aur_count()
            part = 0.0
            if self.building:
                part = min(0.95, (time.time() - self.build_t) / max(5, self.h.aur_time(self.building)))
            return min(1.0, (self.aur_done + part) / n)
        if self.stage == "cleanup":
            return min(0.95, (time.time() - self.stage_t) / max(2, self.h.d["cleanup_sec"]))
        return 0.0

    def overall(self):
        """Weighted progress 0..1 over all stages, using estimated durations as weights."""
        est = self._estimates()
        total = sum(est.values()) or 1
        done = 0.0
        order = self.STAGES
        cur = order.index(self.stage) if self.stage in order else -1
        for i, st in enumerate(order):
            if i < cur:
                done += est[st]
            elif i == cur:
                done += est[st] * self.stage_progress()
        return max(0.0, min(1.0, done / total))

    def _estimates(self):
        n = self.total or self.repo_count
        return {
            "download": (self.dl_bytes / self.h.d["dl_bps"]) if self.dl_bytes else (2.0 if n else 0.0),
            "install": n * self.h.d["sec_per_pkg"],
            "hooks": self.h.d["hooks_sec"] if n else 0.0,
            "aur": sum(self.h.aur_time(p) for p in self.aur_pkgs),
            "cleanup": self.h.d["cleanup_sec"],
        }

    def eta(self):
        est = self._estimates()
        order = self.STAGES
        if self.stage not in order:
            return sum(est.values())
        cur = order.index(self.stage)
        elapsed = time.time() - self.stage_t
        p = self.stage_progress()
        if p > 0.05 and self.stage in ("download", "install", "hooks"):
            remain_cur = elapsed / p * (1 - p)
        else:
            remain_cur = max(0.0, est[self.stage] - elapsed) if est[self.stage] else 0.0
            if self.stage == "aur" and self.building:
                remain_cur = max(0.0, self.h.aur_time(self.building) - (time.time() - self.build_t)) + sum(
                    self.h.aur_time(p) for p in self.aur_pkgs[self.aur_done + 1:])
        return remain_cur + sum(est[st] for st in order[cur + 1:])

    def learn(self):
        """Fold this run's timings into history (implausible values are ignored)."""
        self._go("end")
        t = self.timings
        if self.total and 0.05 * self.total < t.get("install", 0) < 30 * self.total:
            self.h.ema("sec_per_pkg", t["install"] / self.total)
        if self.dl_bytes and 1 < t.get("download", 0) < 7200:
            self.h.ema("dl_bps", self.dl_bytes / t["download"])
        if 0 < t.get("hooks", 0) < 1800:
            self.h.ema("hooks_sec", t["hooks"])
        if 0 < t.get("cleanup", 0) < 120:
            self.h.ema("cleanup_sec", t["cleanup"])
        self.h.d.setdefault("aur", {}).update(self.aur_learned)
        self.h.save()


# ───────────────────────────── views ─────────────────────────────

class TerminalView:
    """Plain terminal: raw arch-update output plus short responder messages."""
    animated = False

    def __init__(self):
        self.interactive = sys.stdin.isatty()

    def out(self, data):
        if TERM_OK:
            view = memoryview(data)
            try:
                while view:
                    view = view[os.write(1, view):]   # SIGWINCH can cut a blocked write short
            except OSError:
                lost_terminal()

    def message(self, msg, color=C_DIM):
        text = f"{color}{msg}{C_OFF}"
        self.out(text.replace("\r\n", "\n").replace("\n", "\r\n").encode())
        if LOG:
            LOG.text(msg)

    def output(self, data):
        self.out(data)

    def bell(self):
        self.out(b"\a")

    def handover(self, why, line):
        self.bell()
        self.message(f"\n[автоответчик] {why}: ответь сам, после Enter я продолжу.\n", C_YEL)
        self.message(line + " ", C_BOLD)

    def handover_done(self):
        pass

    def key(self, data):
        """Translate a key press: return bytes for the responder, or None to swallow it."""
        return data

    def tick(self):
        pass

    def answered(self, label, line, answer):
        pass

    def fallback(self, tb):
        self.message("\n[автоответчик] внутренняя ошибка, дальше отвечай сам:\n" + tb, C_RED)


VIEW = TerminalView()


def say(msg, color=C_DIM):
    VIEW.message(msg, color)


def lost_terminal():
    global TERM_OK
    if not TERM_OK:
        return
    TERM_OK = False
    for fd in (0, 1):
        try:
            nul = os.open(os.devnull, os.O_RDWR)
            os.dup2(nul, fd)
            os.close(nul)
        except OSError:
            pass
    if LOG is None or not RUNNING:   # nothing is running: nothing to protect or announce
        return
    LOG.event("ОКНО", "окно терминала закрыто — продолжаю в фоне, pacman не прерываю")
    notify("Окно Arch-Update закрыто",
           "Обновление не прерываю: довожу до безопасной точки в фоне.\n"
           "Журнал: ~/.local/state/arch-update-auto/logs/latest.log", urgent=True)


# ───────────────────────────── responder ─────────────────────────────

class Responder:
    def __init__(self, child, stop_on_news, manual):
        self.child = child
        self.stop_on_news = stop_on_news
        self.tail = ""            # recent colour-stripped output
        self.mode = "manual" if manual else "auto"   # auto | handover | manual
        self.handover_line = None
        self.user_typed = False   # user pressed keys since the last handover
        self.typed = b""
        self.reading_news = False
        self.saw_new_news = False
        self.notes = set()
        self.failures = set()
        self.hinted = False
        self.forced = False       # Ctrl+] pressed: Ctrl+C reaches the program even mid-transaction
        self.safe_only = False    # window closed during a manual run: never confirm anything for the user
        self.handover_why = ""
        self.secret = False       # the question handed over asks for a password: never show or log the answer
        self.heard = False        # the program has said something: in manual mode keys reach it from then on

    def feed(self, data):
        self.heard = True
        text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        if self.mode == "manual" and TERM_OK and PASSWORD_LINE.search(ANSI.sub("", text)):
            # A password prompt has just come and is not on screen yet: keys still waiting were typed before
            # it, while the program still echoed them into the journal. A password typed ahead goes no further.
            try:
                termios.tcflush(0, termios.TCIFLUSH)
            except (termios.error, OSError, ValueError):
                pass
        # Strip colours on the joined tail: a read can end inside an escape sequence.
        self.tail = ANSI.sub("", self.tail + text)[-6000:]
        recent = self.tail[-(len(text) + 200):]
        if "[NEW]" in recent:
            self.saw_new_news = True
        for m in FAILURE_MARKERS.finditer(recent):
            self.failures.add(m.group(0))
        if self.mode == "handover":
            if not self.user_typed and self.current_line() != self.handover_line:
                self.mode = "auto"    # false alarm: the program moved on by itself
                VIEW.handover_done()
                LOG.event("АВТО", "программа пошла дальше сама — продолжаю автоответы")
            elif self.user_typed and "\n" in text and not self.typed.endswith((b"\r", b"\n")):
                # The program moved on while the user was typing: drop the half-typed answer.
                self.flush_child_input()
                self.mode, self.user_typed = "auto", False
                VIEW.handover_done()
                say("\n[автоответчик] программа ушла дальше — твой ввод отменён.\n", C_YEL)
                LOG.event("АВТО", "программа ушла дальше, недопечатанный ввод сброшен")

    def flush_child_input(self):
        try:
            fd = os.open(os.readlink(f"/proc/{self.child.pid}/fd/0"), os.O_RDWR | os.O_NOCTTY)
            termios.tcflush(fd, termios.TCIFLUSH)
            os.close(fd)
        except OSError:
            pass

    def waiting(self):
        """The cursor sits right after the last output (a prompt), not on a fresh line."""
        return bool(self.tail) and not self.tail.endswith("\n")

    def current_line(self):
        return last_lines(self.tail, 1)[0].strip() if self.tail.strip() else ""

    def decide(self):
        line = self.current_line()
        if not line or not self.waiting():
            return None
        if line == "==>" and any(YAY_MENU.search(l) for l in last_lines(self.tail, 4)):
            return ("yay menu (default)", ENTER, None)
        for label, rx, action, note in RULES:
            if rx.search(line):
                recent = "\n".join(last_lines(self.tail, 4))
                if label == "запрос пароля":
                    if re.search(r"account is locked|left to unlock", recent):
                        label = "после неверных паролей вход заблокирован на несколько минут — подожди или Ctrl+C"
                    elif "Sorry, try again" in recent:
                        label = "пароль не подошёл — попробуй ещё раз (раскладка?)"
                elif label == "скачанный пакет повреждён" and KEYRING_TROUBLE.search(recent):
                    label = "подпись пакета не проверилась — похоже, устарел keyring"
                if action == NEWS:
                    if self.saw_new_news and self.stop_on_news:
                        self.reading_news = True
                        return ("есть новые новости Arch — прочитай", HANDOVER, None)
                    return (label, ENTER, None)
                if label == "press enter to continue" and self.reading_news:
                    self.reading_news = False
                    return ("дочитай новости", HANDOVER, None)
                return (label, action, note)
        return None

    def detach(self):
        """The window closed while the user owned the question (handover/manual): nobody can answer
        it any more. Back to auto, where answer() turns HANDOVER into detached_answer(). A manual run
        was never meant to be auto-confirmed, so from now on it only gets detached (safe) answers."""
        partial = re.split(rb"[\r\n]", self.typed)[-1] if self.user_typed else b""
        if partial:                           # a half-typed answer nobody will finish
            self.flush_child_input()
            echo = partial.decode("utf-8", "replace")
            if self.mode == "handover" and self.handover_line:
                self.tail = self.tail[:self.tail.rfind("\n") + 1] + self.handover_line
            elif self.tail.endswith(echo):
                self.tail = self.tail[:-len(echo)]
        self.safe_only = self.safe_only or self.mode == "manual"
        LOG.event("ФОН", f"окно закрыто в режиме «{self.mode}» — отвечаю сам"
                         + (", только безопасные ответы" if self.safe_only else ""))
        self.mode, self.user_typed, self.typed, self.reading_news = "auto", False, b"", False

    def answer(self, label, action, note):
        if note:
            self.notes.add(note)
        line = self.current_line()
        if self.safe_only and action == YES:
            action = HANDOVER     # -> detached_answer(): 'n' inside pacman, Ctrl+C outside it
        if action == HANDOVER and not TERM_OK:
            action = detached_answer(line)
            if action is None:
                LOG.event("ФОН", f"«{line}» — жду, пока pacman закончит (окно закрыто)")
                return
            label = f"{label} (окно закрыто: безопасный ответ)"
        if action == HANDOVER:
            self.hand_over(label)
            return
        shown = {"\x03": "Ctrl+C"}.get(action, action.strip() or "Enter")
        LOG.event("АВТО", f"«{line}» → {shown}   ({label})")
        VIEW.answered(label, line, action.strip() or "Enter")
        self.child.send(action)
        self.tail = ""

    def hand_over(self, why):
        self.mode = "handover"
        self.user_typed, self.typed = False, b""
        self.handover_line = self.current_line()
        self.handover_why = why
        self.secret = bool(re.search(r"(?i)pass(?:word|phrase)|пароль|\bPIN\b", self.handover_line))
        LOG.event("ТЕБЕ", f"{why}: «{self.handover_line}»")
        VIEW.handover(why, self.handover_line)
        notify("Arch-Update ждёт твоего ответа", f"{why}\n{self.handover_line}", urgent=True)

    def user_input(self, data):
        if b"\x1d" in data:  # Ctrl+]
            data = data.replace(b"\x1d", b"")
            if not self.forced:
                LOG.event("CTRL+]", "Ctrl+C теперь передаётся программе")
            self.forced = True
            if self.mode != "manual":
                self.mode = "manual"
                LOG.event("РУЧНОЙ", "Ctrl+] — ручной режим до конца обновления")
                say("\n[автоответчик] ручной режим до конца обновления.\n", C_YEL)
        if b"\x03" in data and not self.forced and pacman_busy():
            data = data.replace(b"\x03", b"")
            LOG.event("CTRL+C", "не передан: pacman в процессе установки")
            say("\n[автоответчик] pacman устанавливает пакеты: Ctrl+C сейчас оборвал бы его до хуков "
                "(initramfs, NVIDIA, подпись) — не передаю. Копировать: Ctrl+Shift+C. "
                "Прервать всё равно: Ctrl+], затем Ctrl+C.\n", C_YEL)
        if self.mode == "manual" and not self.heard:
            data = b"\x03" if b"\x03" in data else b""      # typed before the program spoke: only Ctrl+C goes
        if self.mode in ("handover", "manual"):
            if data:
                self.user_typed = True
                self.typed += data
                self.child.send(data)
            if self.mode == "handover" and (b"\r" in data or b"\n" in data):
                shown = "•••" if self.secret else self.typed.decode("utf-8", "replace").strip()
                LOG.event("ТЫ", f"ответил: «{shown}»")
                self.mode = "auto"
                self.tail = ""
                VIEW.handover_done()
                if self.reading_news and not self.typed.strip():
                    self.reading_news = False
        elif b"\x03" in data:
            LOG.event("CTRL+C", "передан программе")
            self.child.send(b"\x03")
        elif data and not self.hinted and not VIEW.animated:
            self.hinted = True
            say("\n[автоответчик] сейчас отвечаю я, твой ввод не передаётся. "
                "Если программа ждёт ответа — Ctrl+], дальше отвечай сам.\n", C_YEL)


def detached_answer(line):
    """The window is gone and nobody can answer: never interrupt pacman."""
    if not pacman_busy():
        return "\x03"          # no pacman transaction running (a stale lock doesn't count): end the run
    if re.search(r"\[[YyNn]/[YyNn]\]$", line):
        return NO              # pacman asks only before commit: 'n' aborts or keeps things as they are
    return None


# ───────────────────────────── checks ─────────────────────────────

def not_started(why, verdict):
    """Result of a run that never touched the system (refused before it began)."""
    LOG.event("НЕ НАЧАТО", why)
    notify("Обновление не начато", why, urgent=verdict == 2)
    return {"changed": [], "added": [], "removed": [], "problems": [why] if verdict == 2 else [],
            "warnings": [why] if verdict < 2 else [], "ok": [], "verdict": verdict, "log": LOG.path,
            "exitstatus": None, "started": False, "head": "Обновление не начиналось"}


def system_checks(before, exitstatus, responder, pacman_log_offset, failed_before=()):
    """Post-update checks. Returns a dict; also logs, appends pacman.log excerpt and notifies."""
    after = installed_versions()
    changed = sorted(p for p, v in after.items() if before.get(p) not in (None, v))
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    problems, warnings, ok = [], [], []
    try:
        with open(PACMAN_LOG, encoding="utf-8", errors="replace") as f:
            f.seek(pacman_log_offset)
            pacman_log = f.read()          # what pacman and its hooks did in this update
    except OSError:
        pacman_log = None

    LOG.event("ПАКЕТЫ", "обновлены: " + (", ".join(f"{p} {before[p]}→{after[p]}" for p in changed) or "—"))
    if added or removed:
        LOG.event("ПАКЕТЫ", f"новые: {', '.join(added) or '—'}; удалены: {', '.join(removed) or '—'}")
    if exitstatus not in (0, None):
        problems.append(f"arch-update завершился с кодом {exitstatus}")

    if os.path.exists(PACMAN_LOCK) and not run(["pgrep", "-x", "pacman"]).strip():
        problems.append("остался замок pacman /var/lib/pacman/db.lck — pacman мог прерваться")
    else:
        ok.append("pacman завершился штатно")
    for f in sorted(responder.failures):
        flash = KERNEL_FLASH.search(f)
        if flash:
            problems.append(f"новое ядро не записано в раздел ядра — перед перезагрузкой выполни: sudo {flash.group(0)}")
            continue
        (warnings if "update process" in f else problems).append(f"в выводе: «{f}»")
    if any(KEYRING_TROUBLE.search(f) for f in responder.failures):
        keyring = "archlinuxarm-keyring" if before.get("archlinuxarm-keyring") else "archlinux-keyring"
        problems.append(f"похоже, устарел keyring: sudo pacman -Sy {keyring} && sudo pacman -Su, потом обнови снова")
    for t in unmounted_boot():
        problems.append(f"{t} не смонтирован — ядро могло записаться не туда, НЕ перезагружайся")

    kernels = kernel_packages()
    boot_files, checked, closed, unverified = [], 0, [], False
    for pkg, kver in sorted(kernels.items()):
        files = preset_files(pkg)
        if not files:
            LOG.event("ЗАГРУЗКА", f"{pkg}: нет пресета mkinitcpio — файлы загрузки не проверяю")
            continue
        checked += 1
        for kind, f in files:
            boot_files.append(f)
            try:
                size = os.stat(f).st_size
            except PermissionError:
                closed.append(f)           # /boot closed to users: unknown, not missing
                continue
            except OSError:
                problems.append(f"нет {f} — НЕ перезагружайся, система не загрузится")
                continue
            if size == 0:
                problems.append(f"{f} пустой — НЕ перезагружайся (сначала: sudo mkinitcpio -P)")
            elif kind == "image" and os.access(f, os.R_OK):      # root-only images can't be looked into
                try:
                    r = subprocess.run(["lsinitcpio", f], capture_output=True, text=True, timeout=120)
                    img_ok = r.returncode == 0 and f"usr/lib/modules/{kver}/" in r.stdout
                except (OSError, subprocess.SubprocessError):
                    img_ok = False
                if not img_ok:
                    problems.append(f"{f} обрезан или собран не под ядро {kver} — НЕ перезагружайся "
                                    "(сначала: sudo mkinitcpio -P)")
    if closed:
        sizes = boot_file_sizes_as_root(closed)
        if sizes is not None:
            for f in closed:
                if f not in sizes:
                    problems.append(f"нет {f} — НЕ перезагружайся, система не загрузится")
                elif sizes[f] == 0:
                    problems.append(f"{f} пустой — НЕ перезагружайся (сначала: sudo mkinitcpio -P)")
        else:
            # No look inside: judge by what this update did to the boot files.
            unverified = True
            log = pacman_log or ""
            if not any(p in kernels for p in changed + added) and "mkinitcpio" not in log:
                ok.append("ядро и initramfs этим обновлением не менялись")
            elif "Image generation successful" in log and not FAILURE_MARKERS.search(log):
                ok.append("initramfs собран заново (по выводу mkinitcpio)")
            else:
                warnings.append("не смог заглянуть в /boot: он закрыт для пользователя — "
                                "перед перезагрузкой проверь: sudo ls -l /boot")
    if checked and not unverified and not any("НЕ перезагружайся" in p for p in problems):
        ok.append("ядро и initramfs на месте" if len(kernels) == 1 else f"ядра и initramfs на месте ({checked})")
    # The dkms pacman hook never fails the transaction: a module that didn't build shows up only here.
    for module, kvers in sorted(dkms_built().items()):
        missing = [k for k in sorted(kernels.values()) if k not in kvers and not dkms_skips(module, k)]
        name = "драйвер NVIDIA" if module == "nvidia" else f"модуль DKMS {module}"
        if missing:
            problems.append(f"{name} не собран под ядро {', '.join(missing)} — НЕ перезагружайся")
        elif kernels:
            ok.append(f"{name} собран под ядро")
    running = os.uname().release
    if kernels and running not in kernels.values():
        warnings.append(f"ядро обновилось (сейчас работает {running}) — перезагрузись, когда будет удобно")
    # -n: never asks for a password; no sbctl or Secure Boot off → nothing to check
    sb = run(["sudo", "-n", "sbctl", "verify"]) if secure_boot_on() else ""
    unsigned = [f for f in boot_files if f"{f} is not signed" in sb]
    if unsigned:
        problems.append(f"{', '.join(unsigned)} не подписан(ы) для Secure Boot — НЕ перезагружайся")
    elif any(f"{f} is signed" in sb for f in boot_files):
        ok.append("подпись Secure Boot в порядке")
    elif boot_files and secure_boot_on() and shutil.which("sbctl"):
        # a sudo that asks for a password gave its ticket to the update's terminal, not to us
        warnings.append("подписи Secure Boot проверить не удалось — перед перезагрузкой: sudo sbctl verify")

    if is_chromebook() and {"alsa-ucm-conf", "sof-firmware"} & set(changed + added):
        warnings.append("обновились alsa-ucm-conf/sof-firmware: если пропал звук, снова запусти скрипт "
                        "chromebook-linux-audio")
    reboot = [p for p in changed + added if REBOOT_PKGS.match(p) or p in kernels]
    if reboot:
        warnings.append("перезагрузись, когда будет удобно (обновились: " + ", ".join(reboot[:8]) + ")")
    elif "reboot" in responder.notes and not kernels:      # no kernel of ours to judge by: trust arch-update
        warnings.append("arch-update просит перезагрузиться — перезагрузись, когда будет удобно")
    elif "reboot" in responder.notes:
        LOG.event("ЯДРО", "arch-update предлагал перезагрузку, но работающее ядро на месте — пропускаю")
    if "pacnew" in responder.notes or run(["pacdiff", "-o"]).strip():
        warnings.append("есть .pacnew файлы — разберём вместе (pacdiff -o покажет список)")
    if "orphans" in responder.notes:
        warnings.append("есть пакеты-сироты — не удалял, посмотреть: pacman -Qdtq")
    if "services" in responder.notes:
        warnings.append("arch-update предлагал перезапустить сервисы — пропущено, перезагрузка это решит")
    failed = failed_units()
    new_failed = [u for u in failed if u not in failed_before]
    if new_failed:
        warnings.append("упавшие сервисы: " + ", ".join(new_failed))
    else:
        ok.append("упавших сервисов нет" if not failed else "новых упавших сервисов нет")
    if failed_before:
        LOG.event("СЕРВИСЫ", "не работали ещё до обновления: " + ", ".join(failed_before))

    if LOG.broken:
        warnings.append("журнал обновления неполный: не удалось писать на диск (место в /home?)")
    for p in problems:
        LOG.event("ПРОБЛЕМА", p)
    for w in warnings:
        LOG.event("ВНИМАНИЕ", w)
    if pacman_log is not None:
        LOG.text("\n══════ /var/log/pacman.log за это обновление ══════\n" + pacman_log)

    if problems:
        notify("Обновление: нужна проверка", "\n".join(problems), urgent=True)
    else:
        notify("Обновление завершено", "\n".join(warnings) or "Всё прошло чисто.")
    return {"changed": changed, "added": added, "removed": removed, "problems": problems,
            "warnings": warnings, "ok": ok, "verdict": 2 if problems else (1 if warnings else 0),
            "log": LOG.path, "exitstatus": exitstatus}


# ───────────────────────────── running ─────────────────────────────

def terminal_hung_up(view):
    """True if our terminal is already gone (a pty slave reports POLLHUP once Konsole closed it)."""
    if not view.interactive:
        return False
    try:
        pl = select.poll()
        pl.register(0, select.POLLIN)
        if any(ev & (select.POLLHUP | select.POLLERR | select.POLLNVAL) for _, ev in pl.poll(0)):
            lost_terminal()
            return True
    except (OSError, ValueError):
        pass
    return False


def shield_from_window_close():
    """Konsole closes a window with SIGHUP and then SIGKILL to the process it started, and a pty
    hangup SIGHUPs that session. Do the work in a child in its own session so a closed window can
    never hang up arch-update/pacman mid-transaction. The parent just relays and waits."""
    pid = os.fork()
    if pid == 0:
        os.setsid()
        return
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_IGN)
    signal.signal(signal.SIGWINCH, lambda s, _: os.kill(pid, s))
    while True:
        try:
            _, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    code = os.waitstatus_to_exitcode(status)
    os._exit(code if code >= 0 else 128 - code)


class Feed:
    """Splits raw output into complete, CR-aware, colour-stripped lines for the view/progress.
    pacman ends every redraw with '\r' and the final one with '\r\n' (pty ONLCR -> '\r\r\n')."""
    TOTAL = re.compile(r"Total \(\s*\d+/\d+\).*\d+%\s*$")

    def __init__(self):
        self.buf, self.carry = "", ""

    def push(self, data):
        text = self.carry + data.decode("utf-8", "replace")
        text, self.carry = split_escape(text)   # escape split across reads: finish it next time
        self.buf += ANSI.sub("", text)
        lines = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            lines.append(line.rstrip("\r").split("\r")[-1])
        if len(self.buf) > 16384:          # download bars redraw for minutes without '\n'
            self.buf = "\r".join(self.buf.split("\r")[-12:])
        return lines

    def partial(self):
        segs = [x for x in self.buf.split("\r") if x.strip()]
        return next((x for x in reversed(segs) if self.TOTAL.search(x)), segs[-1] if segs else "")


def pump(child, responder, auto):
    """Copy arch-update <-> view until arch-update closes its pty.
    auto=False is a plain pass-through that never touches the responder logic."""
    stdin_open = TERM_OK
    last_out, pending = time.time(), auto   # after a fallback a question may already be waiting
    forced = False
    while True:
        fds = [child.child_fd] + ([0] if stdin_open and TERM_OK else [])
        try:
            ready, _, _ = select.select(fds, [], [], 1 / 30 if VIEW.animated else 0.1)
        except InterruptedError:
            continue
        if child.child_fd in ready:
            try:
                data = os.read(child.child_fd, 65536)
            except OSError:
                data = b""
            if not data:
                return
            LOG.output(data)
            VIEW.output(data)
            if auto:
                responder.feed(data)
                last_out, pending = time.time(), True
        if 0 in ready:
            try:
                data = os.read(0, 1024)
            except OSError:
                data = b""
            if not data:
                stdin_open = False
                if VIEW.interactive or not TERM_OK:   # a hung-up tty is no longer isatty()
                    lost_terminal()
                    pending = True   # re-evaluate a question that may be waiting
            else:
                data = VIEW.key(data)
                if data:
                    if auto:
                        responder.user_input(data)
                    else:
                        forced = forced or b"\x1d" in data
                        if b"\x03" in data and not forced and pacman_busy():
                            data = data.replace(b"\x03", b"")   # same guard as Responder.user_input
                            LOG.event("CTRL+C", "не передан: pacman в процессе установки")
                        try:
                            child.send(data.replace(b"\x1d", b""))
                        except OSError:
                            pass
        if auto and not TERM_OK and responder.mode != "auto":
            responder.detach()
            pending, last_out = True, 0.0     # re-match the waiting question right now
        elif auto and not TERM_OK and not pending and responder.waiting() and time.time() - last_out > 5:
            pending, last_out = True, time.time() - SETTLE   # window gone, still waiting: decide again
        VIEW.tick()
        if not auto or not pending:
            continue
        idle = time.time() - last_out
        if idle < SETTLE:
            continue
        if responder.mode == "manual":
            # Even in manual mode don't make the user press Enter twice at the end.
            if responder.waiting() and QUIT_PROMPT.search(responder.current_line()):
                pending = False
                responder.answer("press enter to quit", ENTER, None)
            continue
        if responder.mode != "auto":
            continue
        decision = responder.decide()
        if decision:
            pending = False
            responder.answer(*decision)
        elif idle >= UNKNOWN_IDLE:
            line = responder.current_line()
            if responder.waiting() and UNKNOWN_PROMPT.search(line):
                pending = False
                responder.answer("незнакомый вопрос", HANDOVER, None)
            elif not responder.waiting() or not re.search(r"[:?\]>]\s*$", line):
                pending = False                 # output just paused (a build step, a progress line)
            elif idle >= STALL_IDLE and not pacman_busy():
                pending = False
                responder.answer("программа, похоже, ждёт ввода", HANDOVER, None)


def run_update(view, manual=False, stop_on_news=True, cmd=None, summary=True, dims=None, on_spawn=None,
               demo=False):
    """Run arch-update with the given view. Caller sets up the terminal (raw mode etc.).
    on_spawn(child) is called right after the child starts (e.g. to forward window resizes).
    demo=True: cmd is a simulation; nothing about the real system is prepared or guarded.
    Returns (exitstatus, result-dict-or-None, responder)."""
    global LOG, VIEW, RUNNING
    VIEW = view
    if not TERM_OK or terminal_hung_up(view):   # window closed before anything started: start nothing
        return None, None, None
    cmd = cmd or os.environ.get("ARCH_UPDATE_CMD", "arch-update")
    LOG = RunLog()
    LOG.event("СТАРТ", f"{cmd}, режим: {'ручной' if manual else 'автоответы'}, "
                       f"новости: {'останавливаться' if stop_on_news else 'не останавливаться'}, "
                       f"вид: {type(view).__name__}")
    try:
        pacman_log_offset = os.path.getsize(PACMAN_LOG)
    except OSError:
        pacman_log_offset = 0
    before = installed_versions()
    failed_before = failed_units()
    if terminal_hung_up(view):      # closed while we were preparing (pacman -Q can take a moment)
        LOG.event("ОКНО", "окно закрыто до запуска — обновление не начинаю")
        return None, None, None
    argv = shlex.split(cmd)
    sudo_once = False
    if not demo:
        missing = unmounted_boot()
        if missing:
            return None, not_started(f"{', '.join(missing)} не смонтирован — обновление не начинаю "
                                     "(ядро записалось бы не туда)", 2), None
        if elevation_command() == "sudo" and sudo_asks_password():
            # The password once, at the start, while the user is watching: sudo forgets it after
            # 5 minutes, and a long run asks again for the AUR step, the cache cleanup… The loop
            # keeps this terminal's ticket fresh and dies with the run (same session, SIGHUP).
            # 125: the password was refused (or the question cancelled) — nothing has started.
            LOG.event("SUDO", "sudo спрашивает пароль: спрошу один раз в начале")
            sudo_once = True
            argv = ["sh", "-c", 'sudo -v || exit 125; (while sleep 50; do sudo -n -v || exit; done) '
                                '</dev/null >/dev/null 2>&1 & exec "$@"', "sh"] + argv
        if battery() and can_inhibit():
            # a laptop must not fall asleep in the middle of a pacman transaction
            argv = INHIBIT + ["--who=Arch-Update", "--why=обновление системы"] + argv
    cols, rows = dims or shutil.get_terminal_size((120, 40))
    child = pexpect.spawn(argv[0], argv[1:], env=child_env(), dimensions=(rows, cols), echo=True)
    RUNNING = True
    if on_spawn:
        on_spawn(child)
    responder = Responder(child, stop_on_news, manual)
    global RESPONDER
    RESPONDER = responder
    signal.signal(signal.SIGHUP, signal.SIG_IGN)   # our own session never hangs up the work
    try:
        pump(child, responder, auto=True)
    except Exception:
        tb = traceback.format_exc()
        LOG.event("ОШИБКА", tb)
        try:
            view.fallback(tb)
        except Exception:
            pass
        plain = TerminalView()             # the view that failed is not used again; same responder:
        plain.interactive = view.interactive   # keeps the Ctrl+C guard and safe answers on window close
        VIEW = plain
        while True:                        # never leave a live child: pacman may be running
            try:
                pump(child, responder, auto=True)
                break
            except Exception:
                LOG.event("ОШИБКА", traceback.format_exc())
                try:
                    pump(child, responder, auto=False)
                    break
                except Exception:
                    LOG.event("ОШИБКА", traceback.format_exc())
    child.wait()    # pty is at EOF: only reap
    child.close()   # child already exited -> no signals sent
    RUNNING = False
    status = child.exitstatus if child.exitstatus is not None else child.signalstatus
    LOG.event("КОНЕЦ", f"arch-update завершился, код {status}")
    if sudo_once and status == 125:
        return status, not_started("пароль sudo не принят — обновление не начиналось, система не менялась", 1), \
            responder
    if child.exitstatus in NOTHING_DONE and installed_versions() == before:
        return status, not_started(NOTHING_DONE[child.exitstatus], 1), responder
    result = system_checks(before, status, responder, pacman_log_offset, failed_before) if summary else None
    return status, result, responder
