"""Engine shared by arch-update-auto (plain terminal) and arch-update-ui (full-screen UI).

Runs the real arch-update in a pty and answers its routine questions (RULES).
Risky or unknown questions are handed to the user through the active View.
Everything is logged per run; after the run the system is checked (/boot,
initramfs, DKMS, Secure Boot signature) and a structured result is returned.
The checks find out what this machine has (kernels, mkinitcpio presets, DKMS,
sbctl) and skip what it does not have: the same code runs on every machine.

Each run that reaches a result also leaves a summary next to its journal
(<stamp>.json): the menu keeps warning about a failed boot check until the
machine has rebooted or the problem was fixed (unresolved, fix_commands,
recheck_boot, mark_resolved), and the reports list past updates from it.
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
import tempfile
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
    # Enter: none; with the user's setting on, Responder.services_answer gives the numbers of the safe ones
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
# arch-update's restart_services.sh lists what checkservices found under a header ("1 - cups.service"), asks
# which to restart, then says how each restart went.
SERVICES_HEAD = re.compile(r"services? requires? a post upgrade restart")
SERVICES_PROMPT = re.compile(r"Select the service\(s\) to restart")
SERVICE_ITEM = re.compile(r"^(\d+) - (\S+)$")
SERVICE_DONE = re.compile(r"The (\S+) service has been successfully restarted")
SERVICE_FAILED = re.compile(r"An error has occurred during the restart of the (\S+) service")
SERVICES_ALL_DONE = "Service(s) restarted successfully"
SERVICES_ERROR = "An error has occurred during the service(s) restart"
# Services never restarted while the machine runs, the setting on or off: they restart with the next boot.
# checkservices (archlinux-contrib; arch-update runs it as `checkservices -F -P -R -i <display managers>`) leaves
# out by itself getty@tty*, systemd-logind, dbus-broker and auditd, and every service in a user's or a container's
# slice (user@.service, user-runtime-dir@.service among them); the rest of what it lists is anything that still runs
# a replaced binary or library. This list is kept in case checkservices or arch-update change, and adds what a
# restart under a running desktop session would break.
KEEP_RUNNING = re.compile(r"^(?:" + "|".join([
    # the system bus and the seats: every session holds on to them, a restart ends the desktop session
    r"dbus", r"dbus-broker", r"dbus-daemon", r"systemd-logind", r"elogind", r"seatd",
    # the user's own session: its systemd and /run/user, logins on the consoles, homed home folders
    r"user@.*", r"user-runtime-dir@.*", r"(?:serial-|container-)?getty@.*", r"console-getty", r"autovt@.*",
    r"kmsconvt?@.*", r"systemd-homed",
    # display managers: the graphical session runs under them
    r"display-manager", r"gdm", r"sddm", r"plasmalogin", r"plasma-login", r"lightdm", r"lxdm", r"slim", r"xdm",
    r"greetd", r"nodm", r"ly(?:@.*)?", r"lemurs", r"entrance", r"emptty", r"cosmic-greeter", r"xlogin@.*",
    # the session's password prompts
    r"polkit",
    # refuses a restart by hand
    r"auditd",
    # the way to the network: Wi-Fi on the Chromebooks, an SSH session, the update report still to go out;
    # one that did not come back would leave the machine offline until a reboot
    r"NetworkManager", r"wpa_supplicant(?:@.*)?", r"iwd", r"systemd-networkd", r"dhcpcd(?:@.*)?", r"connman",
    r"netctl(?:-auto)?@.*", r"ModemManager", r"tailscaled", r"zerotier-one", r"openvpn.*", r"strongswan.*",
    r"wg-quick@.*",
    # the keyboard and the mouse in use (Bluetooth, key remappers like keyd on Chromebooks), a shared desktop
    r"bluetooth", r"keyd", r"kanata.*", r"kmonad.*", r"input-remapper.*", r"udevmon", r"xremap.*",
    r"xrdp(?:-sesman)?", r"gnome-remote-desktop", r"teamviewerd", r"anydesk", r"rustdesk", r"x0vncserver.*",
    r"vncserver.*", r"x11vnc.*",
    # NVIDIA's daemons: the new driver works only with its new kernel module, after a reboot
    r"nvidia.*",
    # containers and machines go down with them, and not all of them come back by themselves
    r"docker", r"systemd-nspawn@.*", r"libvirt-guests",
    # the disks: these set up encrypted disks, swap and file systems once at boot (never listed as running;
    # here in case a later arch-update lists more than running services)
    r"systemd-cryptsetup@.*", r"systemd-zram-setup@.*", r"systemd-fsck@.*", r"systemd-remount-fs",
]) + r")\.service$")
PACNEW_MADE = re.compile(r"warning: (\S+) installed as (\S+\.pacnew)\s*$", re.M)   # pacman.log, this update

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
SETTINGS = os.path.join(STATE_DIR, "settings.json")   # written by report.py (and the settings screen)
BOOT_ID = "/proc/sys/kernel/random/boot_id"           # changes with every boot
CGROUP = "/proc/self/cgroup"
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
RUN_MODE = "auto"  # how the current run was started: "auto" or "manual"


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


def read_json(path):
    """A JSON object from a file, {} if there is none or it is damaged."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    """Atomically: a reader never sees half a file, a full disk leaves none behind. Raises OSError."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_settings():
    """The user's settings (settings.json in the state folder: reports, restart_services …), {} without any."""
    return read_json(SETTINGS)


def boot_id():
    """This boot's random ID: another one after every reboot. "" if it cannot be read."""
    try:
        with open(BOOT_ID) as f:
            return f.read().strip()
    except OSError:
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


# Read-only looks at the boot files as root, through one `sudo -n` (it never asks anything): the sizes of the
# boot files (a /boot closed to users, as archinstall mounts the ESP: fmask/dmask 0077), which kernel an initramfs
# the user may not read was built for, and what sbctl says about the signatures. Run by the update's own wrapper
# right after arch-update, while that session's sudo ticket is valid (a sudo that asks for the password gives its
# ticket to the update's terminal only), or directly when sudo asks nothing. All of it runs in one root shell,
# whose first line says it got that far: a sudo that may not run it (sudoers allows other commands only) leaves
# nothing, never a file that looks missing. It prints:
#   sudo-ok, file <path> (looked at), size <bytes> <path> (it exists), image <lsinitcpio status> <kver,kver|-> <path>,
#   sbctl (asked), sbctl <line of its output>
# Arguments: <sbctl 0|1> <n> <boot file>... <m> <image>...
PROBE = r"""
sb=$1 n=$2
shift 2
files=("${@:1:n}")
shift "$n"
n=$1
shift
images=("${@:1:n}")
echo sudo-ok
for f in "${files[@]}"; do
    printf 'file %s\n' "$f"
    stat -c 'size %s %n' -- "$f" 2>/dev/null
done
for f in "${images[@]}"; do
    l=$(timeout 60 lsinitcpio "$f" 2>/dev/null)
    rc=$?
    k=$(printf '%s\n' "$l" | sed -n 's|^usr/lib/modules/\([^/][^/]*\)/.*|\1|p' | sort -u | paste -sd, -)
    printf 'image %s %s %s\n' "$rc" "${k:--}" "$f"
done
if [ "$sb" = 1 ]; then
    echo sbctl
    timeout 60 sbctl verify 2>/dev/null | sed 's/^/sbctl /'
fi
"""
# sudo asks for the password once, at the start, while the user is watching: it forgets it after 5 minutes,
# and a long run asks again for the AUR step, the cache cleanup… The loop keeps the session's ticket fresh and
# dies with the run (same session, SIGHUP). 125: the password was refused (or the question cancelled).
ASK_SUDO_FIRST = """sudo -v || exit 125
(while sleep 50; do sudo -n -v || exit; done) </dev/null >/dev/null 2>&1 &
"""


def probe_args(files, images, sbctl):
    return ["1" if sbctl else "0", str(len(files)), *files, str(len(images)), *images]


def probe_command(argv, out, files, images, sbctl, ask_first=False):
    """argv run by a bash that afterwards, in the same session (so with its sudo ticket), probes the boot files
    into the file out, silently: never a question, nothing on the screen, no error. It ends as argv ended: with
    the same exit status, or killed by the same signal. ask_first: the sudo password first (ASK_SUDO_FIRST)."""
    script = r"""
probe=$1 out=$2 sb=$3 nf=$4
shift 4
files=("${@:1:nf}")
shift "$nf"
ni=$1
shift
images=("${@:1:ni}")
shift "$ni"
""" + (ASK_SUDO_FIRST if ask_first else "") + r""""$@"
s=$?
trap '' INT QUIT TSTP
# into the file as the user (no root-owned file left behind); a key pressed meanwhile changes nothing
sudo -n bash -c "$probe" probe "$sb" "${#files[@]}" "${files[@]}" "${#images[@]}" "${images[@]}" \
    2>/dev/null </dev/null >"$out"
trap - INT QUIT TSTP
if [ "$s" -gt 128 ] && [ "$s" -lt 192 ]; then
    kill -"$((s - 128))" "$$" 2>/dev/null
fi
exit "$s"
"""
    return ["bash", "-c", script, "arch-update-probe", PROBE, out] + probe_args(files, images, sbctl) + list(argv)


def probe_now(files, images, sbctl):
    """The same probe, right now, from this process: works when sudo asks nothing (or this terminal has a ticket)."""
    return parse_probe(run(["sudo", "-n", "bash", "-c", PROBE, "probe"] + probe_args(files, images, sbctl),
                           timeout=300))


def read_probe(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_probe(f.read())
    except (OSError, TypeError):
        return None


def parse_probe(text):
    """What a probe printed: None when sudo would have asked (nothing was looked at), else {"files": {paths looked
    at}, "sizes": {path: bytes} of those that exist, "images": {path: (lsinitcpio's status, {kernel versions
    inside})}, "sbctl": its output, None if not asked}."""
    lines = (text or "").splitlines()
    if lines[:1] != ["sudo-ok"]:
        return None
    probe, sb = {"files": set(), "sizes": {}, "images": {}, "sbctl": None}, None
    for line in lines[1:]:
        kind, _, rest = line.partition(" ")
        if kind == "file" and rest:
            probe["files"].add(rest)
        elif kind == "size":
            size, _, path = rest.partition(" ")
            if size.isdigit() and path:
                probe["sizes"][path] = int(size)
        elif kind == "image":
            parts = rest.split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit():
                probe["images"][parts[2]] = (int(parts[0]), set(parts[1].split(",")) - {"-", ""})
        elif kind == "sbctl":
            sb = (sb + [rest]) if sb is not None else []
    if sb is not None:
        probe["sbctl"] = "\n".join(sb)
    return probe


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


def running_kernel():
    return os.uname().release


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


def orphans():
    """Packages installed as dependencies that nothing needs any more (what arch-update offers to remove)."""
    return set(run(["pacman", "-Qtdq"]).split())


def pacnew_files():
    """.pacnew/.pacsave files waiting to be merged, from whichever update they are."""
    return [line.strip() for line in run(["pacdiff", "-o"]).splitlines() if line.strip()]


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

def logs_dir():
    """Where the journals (and the summaries next to them) are: the state folder's logs/, or the folder
    ARCH_UPDATE_AUTO_LOGDIR names (the demo's own demo-logs/: the demo never sees or changes the real ones)."""
    return os.environ.get("ARCH_UPDATE_AUTO_LOGDIR", os.path.join(STATE_DIR, "logs"))


class RunLog:
    """Per-run log files: readable .log (CR-aware, colour-stripped, with events) and raw .raw; the run's
    summary (.json) is written next to them once there is a result (see save_summary)."""
    TOTAL_SNAPSHOT = re.compile(r"^\s*Total \(\s*\d+/\d+\)")

    def __init__(self):
        self.dir = logs_dir()
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
            for f in (old, old[:-4] + ".raw", old[:-4] + ".json"):
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

def display_manager():
    """The unit display-manager.service stands for here (sddm.service …), "" without one."""
    unit = run(["systemctl", "show", "--property=Id", "--value", "display-manager.service"], timeout=5).strip()
    return "" if unit == "display-manager.service" else unit


def own_service():
    """The system service this program itself runs in (a kiosk session started as a service), "" if none."""
    try:
        with open(CGROUP) as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"/system\.slice/(?:[^/\n]+/)*?([^/\n]+\.service)(?:/|$)", text, re.M)
    return m.group(1) if m else ""


def keeps_running(unit, dm=None, own=None):
    """A service that is never restarted while the machine runs (KEEP_RUNNING, the display manager, the one this
    program runs in, or a name that is no plain service name): it restarts with the next boot."""
    if not re.fullmatch(r"[\w:.@\\-]+\.service", unit or ""):
        return True
    dm = display_manager() if dm is None else dm
    own = own_service() if own is None else own
    return bool(KEEP_RUNNING.match(unit)) or unit in (dm, own)


def parse_services(tail):
    """The services arch-update listed above its question: ([(number, name)], whole list seen)."""
    lines = tail.split("\n")
    head = max((i for i, line in enumerate(lines) if SERVICES_HEAD.search(line)), default=None)
    found = []
    for line in lines[(head + 1 if head is not None else 0):]:
        m = SERVICE_ITEM.match(line.strip())
        if m:
            found.append((int(m.group(1)), m.group(2)))
    whole = head is not None and [n for n, _ in found] == list(range(1, len(found) + 1))
    return found, whole


def unit_list(units, limit=8):
    """cups, sshd и ещё 3 — names as a person reads them, not as systemctl wants them."""
    names = [u.removesuffix(".service") for u in units]
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" и ещё {more}" if more > 0 else "")


# What the summary says about the services (one or more of them).
def restarted_text(units):
    return ("перезапущен сервис: " if len(units) == 1 else "перезапущены сервисы: ") + unit_list(units)


def waiting_text(units):
    return ("сервис ждёт перезапуска: " if len(units) == 1 else "сервисы ждут перезапуска: ") + unit_list(units)


def not_restarted_text(units, look):
    return ("не перезапустился сервис: " if len(units) == 1 else "не перезапустились сервисы: ") \
        + f"{unit_list(units)} — посмотри: {look}"


def later_text(units):
    return unit_list(units) + (" перезапустится после перезагрузки — на ходу его не трогаю" if len(units) == 1
                               else " перезапустятся после перезагрузки — на ходу их не трогаю")


RESTARTED_TEXTS = ("перезапущен сервис:", "перезапущены сервисы:")
WAITING_TEXTS = ("сервис ждёт перезапуска:", "сервисы ждут перезапуска:")


class Responder:
    def __init__(self, child, stop_on_news, manual, restart_services=False):
        self.child = child
        self.stop_on_news = stop_on_news
        # the user's setting: restart the services that are safe to restart when arch-update asks
        self.restart_services = restart_services
        # what arch-update listed and how the restarts went (system_checks makes res["services"] from it)
        self.services = {"listed": [], "asked": [], "done": set(), "failed": set(), "all_done": False,
                         "error": False}
        self._dm = self._own = None
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
        if "restart" in recent:
            self.watch_services(recent)
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

    def watch_services(self, recent):
        """arch-update's list of services to restart (once its question shows, whoever answers it) and what it
        said about each restart afterwards."""
        svc = self.services
        if self.waiting() and SERVICES_PROMPT.search(self.current_line()):
            found, _ = parse_services(self.tail)
            if found:
                svc["listed"] = [name for _, name in found]
        svc["done"].update(m.group(1) for m in SERVICE_DONE.finditer(recent))
        svc["failed"].update(m.group(1) for m in SERVICE_FAILED.finditer(recent))
        svc["all_done"] = svc["all_done"] or SERVICES_ALL_DONE in recent
        svc["error"] = svc["error"] or SERVICES_ERROR in recent

    def keeps_running(self, unit):
        if self._dm is None:
            self._dm, self._own = display_manager(), own_service()
        return keeps_running(unit, self._dm, self._own)

    def services_answer(self, label, action, note):
        """The answer to "Select the service(s) to restart": Enter (none) unless the user's setting says to restart
        them; then the numbers of those safe to restart while the machine runs, 0 when that is all of them. Only
        for a list read whole, from its header on (a number is arch-update's line number): else Enter. Never in a
        manual run gone unattended (safe_only)."""
        found, whole = parse_services(self.tail)
        if found:
            self.services["listed"] = [name for _, name in found]
        if not self.restart_services or self.safe_only or not whole:
            return label, action, note
        safe = [(n, name) for n, name in found if not self.keeps_running(name)]
        if not safe:
            return label, action, note
        self.services["asked"] = [name for _, name in safe]
        answer = "0" if len(safe) == len(found) else " ".join(str(n) for n, _ in safe)
        return "restart safe services", answer + "\r", note

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
                if label == "restart services":
                    return self.services_answer(label, action, note)
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


# ───────────────────────────── summaries ─────────────────────────────

# The summary of a run, <stamp>.json next to its journal <stamp>.log (pruned with it, see RunLog):
#   verdict, changed, added, removed, problems, warnings, notes, ok, exitstatus, started, head (not started),
#   mode ("auto" | "manual"), began, ended (ISO, local time), boot_id (the boot it ran in),
#   services {listed, restarted, skipped, waiting, later, failed}, fixes [{key, label, argv}],
#   boot [{kind, text, …}] (the problems with booting, see fix_commands), reboot (a reboot would apply the update),
#   later on: rechecked (recheck_boot), resolved (mark_resolved).
SUMMARY_KEYS = ("verdict", "changed", "added", "removed", "problems", "warnings", "notes", "ok", "exitstatus",
                "started", "head", "services", "fixes", "boot", "reboot")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def summary_path(log):
    return os.path.splitext(os.path.realpath(log))[0] + ".json"


def save_summary(res, responder=None):
    """Write the run's summary next to its journal. Never raises: nothing may stop the update's result."""
    log = (res or {}).get("log")
    if not log:
        return
    data = {k: res[k] for k in SUMMARY_KEYS if k in res}
    manual = RUN_MODE == "manual" or getattr(responder, "mode", None) == "manual"
    try:
        began = datetime.datetime.strptime(os.path.basename(os.path.realpath(log))[:19], "%Y-%m-%d_%H-%M-%S")
        began = began.isoformat()
    except ValueError:
        began = now_iso()
    data.update(mode="manual" if manual else "auto", began=began, ended=now_iso(), boot_id=boot_id())
    try:
        write_json(summary_path(log), data)
    except OSError:
        pass


def last_update(logdir=None):
    """The newest update's summary, with "log": its journal; None before the first one."""
    return next(summaries(logdir), None)


def summaries(logdir=None):
    """The summaries of the updates in the journal folder, newest first, each with "log": its journal."""
    for path in sorted(glob.glob(os.path.join(logdir or logs_dir(), "20*.json")), reverse=True):
        data = read_json(path)
        if data:
            data["log"] = path[:-len(".json")] + ".log"
            yield data


def unresolved(logdir=None):
    """The newest update, if it found problems (verdict 2) in this very boot and they were not marked fixed:
    until a reboot or a fix the menu keeps saying so. A run that did not start changed nothing: the update
    before it counts (unless that run is red itself, an unmounted /boot). None otherwise."""
    this_boot = boot_id()
    for data in summaries(logdir):
        if data.get("boot_id", "") != this_boot:
            return None
        if data.get("started", True) or data.get("verdict") == 2:
            return data if data.get("verdict") == 2 and not data.get("resolved") else None
    return None


def journal_event(log, kind, msg):
    """An event line in the journal of that update, also after it has ended."""
    if not getattr(getattr(LOG, "txt", None), "closed", True) and os.path.realpath(LOG.path) == os.path.realpath(log):
        LOG.event(kind, msg)
        return
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%H:%M:%S} {kind}] {msg}\n")
    except (OSError, TypeError):
        pass


def _update_summary(log, change):
    """Read that update's summary, let change(data) alter it, recompute the verdict, write it back.
    Returns the summary, None if there is none or it cannot be written."""
    path = summary_path(log)
    data = read_json(path)
    if not data:
        return None
    change(data)
    if data.get("started", True):
        data["verdict"] = 2 if data.get("problems") else (1 if data.get("warnings") else 0)
    elif not data.get("problems") and data.get("verdict") == 2:
        data["verdict"] = 1                      # not started: what stopped it is gone, it still did not run
    try:
        write_json(path, data)
    except OSError:
        return None
    data["log"] = log
    return data


def mark_resolved(log):
    """The problems of that update are fixed (a fix went through and recheck_boot found nothing more): the
    menu stops warning about them. False if its summary could not be updated."""
    done = _update_summary(log, lambda d: d.update(resolved=now_iso())) is not None
    if done:
        journal_event(log, "ИСПРАВЛЕНО", "проблемы этого обновления отмечены исправленными")
    return done


# ───────────────────────────── checks ─────────────────────────────

def _file_issue(kind, path, pkg, how, text):
    """A boot file missing, empty or broken (how): the kernel file, or an initramfs/UKI image."""
    return {"kind": "kernel" if kind == "kernel" else "image", "pkg": pkg, "path": path, "how": how, "text": text}


def _when(iso):
    """12:34 today, 03.11 12:34 another day."""
    try:
        t = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{t:%H:%M}" if t.date() == datetime.date.today() else f"{t:%d.%m %H:%M}"


def _event(kind, msg):
    if LOG is not None:
        LOG.event(kind, msg)


def _exists(path):
    """False only if the file is surely gone (a folder the user may not look into does not count)."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def boot_state(probe=None, pacman_log=None, changed=(), earlier=(), since=None, found=(), event=_event):
    """The boot partitions, the installed kernels' boot files, their DKMS modules and the Secure Boot signatures,
    as they are now. Returns (issues, warnings, ok, kernels): issues are the problems with booting as dicts
    {"kind", "text", …} (see fix_commands), kernels {package: version}.
    probe: what a probe found as root after the update (parse_probe); without one, or for what it did not look
    at, a probe is tried now (it works when sudo asks nothing). pacman_log: this update's part of pacman.log, to
    judge a /boot nobody could look into by what the update did to it. earlier: issues found before (by an
    earlier update of this boot, or by this one's own check); those this look can neither confirm nor rule out
    stay, as found then (since: when that was, None for this update's own). found: issues this update has
    found already (in its output), so that an earlier one of the same kind is not said twice."""
    issues, warnings, ok, unverified, looked_into = [], [], [], set(), set()
    for t in unmounted_boot():
        issues.append({"kind": "unmounted", "target": t,
                       "text": f"{t} не смонтирован — ядро могло записаться не туда, НЕ перезагружайся"})
    kernels = kernel_packages()
    entries, checked = [], 0                    # (kind, path, package, kernel version)
    for pkg, kver in sorted(kernels.items()):
        files = preset_files(pkg)
        if not files:
            event("ЗАГРУЗКА", f"{pkg}: нет пресета mkinitcpio — файлы загрузки не проверяю")
            continue
        checked += 1
        entries += [(kind, f, pkg, kver) for kind, f in files]
    boot_files = list(dict.fromkeys(f for _, f, _, _ in entries))
    known = {}                                  # path → size, None: no such file; not in it: nobody could look
    for f in boot_files:
        try:
            known[f] = os.stat(f).st_size
        except PermissionError:
            pass                                # /boot closed to users: unknown, not missing
        except OSError:
            known[f] = None
    readable = {f for f in boot_files if known.get(f) and os.access(f, os.R_OK)}
    need_files = [f for f in boot_files if f not in known]
    need_images = list(dict.fromkeys(f for kind, f, _, _ in entries
                                     if kind == "image" and f not in readable and known.get(f, 1)))
    sb_wanted = bool(boot_files) and secure_boot_on() and bool(shutil.which("sbctl"))
    probes = [probe] if probe else []
    if (any(not any(f in p["files"] for p in probes) for f in need_files)
            or any(not any(f in p["images"] for p in probes) for f in need_images)
            or (sb_wanted and not any(p["sbctl"] is not None for p in probes))):
        now = probe_now(need_files, need_images, sb_wanted)
        if now:
            probes.append(now)
    for f in need_files:
        p = next((p for p in probes if f in p["files"]), None)
        if p:
            known[f] = p["sizes"].get(f)
    for kind, f, pkg, kver in entries:
        if f not in known:
            unverified.add("files")
            continue
        size = known[f]
        if size is None:
            issues.append(_file_issue(kind, f, pkg, "missing", f"нет {f} — НЕ перезагружайся, система не загрузится"))
        elif size == 0:
            issues.append(_file_issue(kind, f, pkg, "empty", f"{f} пустой — НЕ перезагружайся (сначала: "
                                      + (f"sudo pacman -S {pkg})" if kind == "kernel" else "sudo mkinitcpio -P)")))
        elif kind == "image":
            if f in readable:
                try:
                    r = subprocess.run(["lsinitcpio", f], capture_output=True, text=True, timeout=120)
                    img_ok = r.returncode == 0 and f"usr/lib/modules/{kver}/" in r.stdout
                except (OSError, subprocess.SubprocessError):
                    img_ok = False
            else:
                p = next((p for p in probes if f in p["images"]), None)
                if p is None:
                    unverified.add("images")    # a root-only image: its size is all anybody could see
                    continue
                rc, kvers = p["images"][f]
                img_ok = rc == 0 and kver in kvers
            looked_into.add(f)
            if not img_ok:
                issues.append(_file_issue(kind, f, pkg, "broken", f"{f} обрезан или собран не под ядро {kver} — "
                                                                  "НЕ перезагружайся (сначала: sudo mkinitcpio -P)"))
    rebuilt = bool(pacman_log) and "Image generation successful" in pacman_log \
        and "Initcpio image generation FAILED" not in pacman_log
    if "files" in unverified:
        # No look inside: judge by what this update did to the boot files.
        if pacman_log is not None and not any(p in kernels for p in changed) and "mkinitcpio" not in pacman_log:
            ok.append("ядро и initramfs этим обновлением не менялись")
        elif rebuilt and not FAILURE_MARKERS.search(pacman_log):
            ok.append("initramfs собран заново (по выводу mkinitcpio)")
        else:
            warnings.append("не смог заглянуть в /boot: он закрыт для пользователя — "
                            "перед перезагрузкой проверь: sudo ls -l /boot")
    elif checked and not any(i["kind"] in ("unmounted", "kernel", "image") for i in issues):
        ok.append("ядро и initramfs на месте" if len(kernels) == 1 else f"ядра и initramfs на месте ({checked})")
    # The dkms pacman hook never fails the transaction: a module that didn't build shows up only here.
    for module, kvers in sorted(dkms_built().items()):
        missing = [k for k in sorted(kernels.values()) if k not in kvers and not dkms_skips(module, k)]
        name = "драйвер NVIDIA" if module == "nvidia" else f"модуль DKMS {module}"
        if missing:
            issues.append({"kind": "dkms", "module": module, "kvers": missing,
                           "text": f"{name} не собран под ядро {', '.join(missing)} — НЕ перезагружайся"})
        elif kernels:
            ok.append(f"{name} собран под ядро")
    # sbctl's verdict: from a probe, else `sudo -n` (it never asks; no sbctl or Secure Boot off: nothing to check)
    sb = next((p["sbctl"] for p in probes if p["sbctl"] is not None), None)
    if sb is None:
        sb = run(["sudo", "-n", "sbctl", "verify"]) if secure_boot_on() else ""
    unsigned = [f for f in boot_files if f"{f} is not signed" in sb]
    if unsigned:
        issues.append({"kind": "unsigned", "files": unsigned,
                       "text": f"{', '.join(unsigned)} не подписан(ы) для Secure Boot — НЕ перезагружайся"})
    elif any(f"{f} is signed" in sb for f in boot_files):
        ok.append("подпись Secure Boot в порядке")
    elif sb_wanted:
        # a sudo that asks for a password gave its ticket to the update's terminal, not to us
        unverified.add("sb")
        warnings.append("подписи Secure Boot проверить не удалось — перед перезагрузкой: sudo sbctl verify")
    # What was found before stays, unless this look saw that very thing again (then what it found counts).

    def seen_again(i):
        kind, path = i.get("kind"), i.get("path")
        if kind in ("unmounted", "dkms"):
            return True                         # looked at every time
        if kind in ("kernel", "image"):
            return path in looked_into if i.get("how") == "broken" else path in known
        if kind == "initcpio":                  # mkinitcpio failed then: a new image built since, or all looked into
            return rebuilt or not ({"files", "images"} & unverified)
        if kind == "unsigned":
            return "sb" not in unverified
        return False                            # a kernel partition to flash by hand: nothing here can tell
    now = {(i["kind"], i.get("path")) for i in list(issues) + list(found)}
    for i in earlier:
        if not isinstance(i, dict) or not isinstance(i.get("text"), str) or seen_again(i):
            continue
        key = (i.get("kind"), i.get("path"))
        if key in now:
            continue                            # found again just now, in other words
        now.add(key)
        kept = dict(i)
        if since:                               # an earlier update's: said as found then
            kept.setdefault("since", since)
            kept.setdefault("found", i["text"])
            kept["text"] = f"не исправлено после обновления в {kept['since']}: {kept['found']}"
        issues.append(kept)
    if any(i.get("kind") in ("kernel", "image", "initcpio") for i in issues):
        ok = [o for o in ok if not re.match(r"(?:ядро|ядра) и initramfs", o)]   # not with a problem still there
    return issues, warnings, ok, kernels


def services_record(responder=None, failed_now=()):
    """res["services"] from what the responder saw: {"listed": what arch-update listed, "restarted", "failed",
    "skipped": the rest, "waiting": skipped ones safe to restart now (services_command), "later": skipped ones
    that restart with the next boot (keeps_running)}."""
    seen = getattr(responder, "services", None) or {}
    listed = list(dict.fromkeys(seen.get("listed") or []))
    asked = list(seen.get("asked") or [])
    done, failed = set(seen.get("done") or ()), set(seen.get("failed") or ())
    if seen.get("all_done") and not done:       # "0": all at once, and it went well
        done = set(asked or listed)
    if seen.get("error") and not done and not failed:
        tried = asked or listed                 # "0" and one of them failed: the failed state says which
        failed = {u for u in tried if u in failed_now}
        done = set(tried) - failed
    skipped = [u for u in listed if u not in done and u not in failed]
    if hasattr(responder, "keeps_running"):
        keep = responder.keeps_running
    else:
        dm, own = (display_manager(), own_service()) if skipped else ("", "")

        def keep(unit):
            return keeps_running(unit, dm, own)
    return {"listed": listed, "restarted": [u for u in listed if u in done and u not in failed],
            "skipped": skipped, "waiting": [u for u in skipped if not keep(u)],
            "later": [u for u in skipped if keep(u)], "failed": sorted(failed)}


def not_started(why, verdict, boot=None):
    """Result of a run that never touched the system (refused before it began). boot: the problems with
    booting that stopped it (an unmounted /boot), for fix_commands."""
    LOG.event("НЕ НАЧАТО", why)
    notify("Обновление не начато", why, urgent=verdict == 2)
    res = {"changed": [], "added": [], "removed": [], "problems": [why] if verdict == 2 else [],
           "warnings": [why] if verdict < 2 else [], "notes": [], "ok": [], "verdict": verdict, "log": LOG.path,
           "exitstatus": None, "started": False, "head": "Обновление не начиналось", "services": services_record(),
           "boot": list(boot or []), "reboot": False}
    res["fixes"] = fix_commands(res)
    save_summary(res)
    return res


def system_checks(before, exitstatus, responder, pacman_log_offset, failed_before=(), orphans_before=None,
                  probe=None, carry=True):
    """Post-update checks. Returns a dict; also logs, appends pacman.log excerpt, notifies and leaves the
    run's summary next to its journal. Problems make the verdict red, warnings yellow; notes (what is not this
    update's problem, or needs nothing now) never change it.
    probe: what the update's wrapper found as root afterwards (parse_probe); orphans_before: `pacman -Qtdq`
    before the update (None: not known); carry: keep the problems with booting an earlier update of this boot
    found and this one cannot rule out (not in the demo)."""
    after = installed_versions()
    changed = sorted(p for p, v in after.items() if before.get(p) not in (None, v))
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    problems, warnings, notes, ok, boot = [], [], [], [], []
    seen_notes = getattr(responder, "notes", set())
    try:
        with open(PACMAN_LOG, encoding="utf-8", errors="replace") as f:
            f.seek(pacman_log_offset)
            pacman_log = f.read()          # what pacman and its hooks did in this update
    except OSError:
        pacman_log = None

    LOG.event("ПАКЕТЫ", "обновлены: " + (", ".join(f"{p} {before[p]}→{after[p]}" for p in changed) or "—"))
    if added or removed:
        LOG.event("ПАКЕТЫ", f"новые: {', '.join(added) or '—'}; удалены: {', '.join(removed) or '—'}")
    if exitstatus not in (0, None, 11):     # 11: a service did not restart, everything else was done (below)
        problems.append(f"arch-update завершился с кодом {exitstatus}")

    if os.path.exists(PACMAN_LOCK) and not run(["pgrep", "-x", "pacman"]).strip():
        problems.append("остался замок pacman /var/lib/pacman/db.lck — pacman мог прерваться")
    else:
        ok.append("pacman завершился штатно")
    for f in sorted(responder.failures):
        flash = KERNEL_FLASH.search(f)
        if flash:
            boot.append({"kind": "flash", "cmd": flash.group(0),
                         "text": "новое ядро не записано в раздел ядра — перед перезагрузкой выполни: "
                                 f"sudo {flash.group(0)}"})
        elif "update process" in f:
            warnings.append(f"в выводе: «{f}»")
        elif "image generation failed" in f.lower():
            boot.append({"kind": "initcpio", "text": f"в выводе: «{f}»"})
        else:
            problems.append(f"в выводе: «{f}»")
    if any(KEYRING_TROUBLE.search(f) for f in responder.failures):
        keyring = "archlinuxarm-keyring" if before.get("archlinuxarm-keyring") else "archlinux-keyring"
        problems.append(f"похоже, устарел keyring: sudo pacman -Sy {keyring} && sudo pacman -Su, потом обнови снова")

    prev = unresolved(LOG.dir) if carry else None       # this run's own summary is not written yet
    if prev and os.path.realpath(prev["log"]) == os.path.realpath(LOG.path):
        prev = None
    issues, w, o, kernels = boot_state(probe, pacman_log, changed + added, earlier=(prev or {}).get("boot") or (),
                                       since=prev and _when(prev.get("began")) or None, found=boot)
    boot += issues
    problems += [i["text"] for i in boot]
    warnings += w
    ok += o

    kernel_new = bool(kernels) and running_kernel() not in kernels.values()
    reboot_pkgs = [p for p in changed + added if REBOOT_PKGS.match(p) or p in kernels]
    if kernel_new:
        notes.append("обновилось ядро — перезагрузись, когда будет удобно"
                     + (f" (обновились: {', '.join(reboot_pkgs[:8])})" if reboot_pkgs else ""))
    elif reboot_pkgs:
        notes.append("перезагрузись, когда будет удобно (обновились: " + ", ".join(reboot_pkgs[:8]) + ")")
    elif "reboot" in seen_notes and not kernels:      # no kernel of ours to judge by: trust arch-update
        notes.append("arch-update советует перезагрузиться — когда будет удобно")
    elif "reboot" in seen_notes:
        LOG.event("ЯДРО", "arch-update предлагал перезагрузку, но работающее ядро на месте — пропускаю")
    reboot = kernel_new or bool(reboot_pkgs) or ("reboot" in seen_notes and not kernels)
    if is_chromebook() and {"alsa-ucm-conf", "sof-firmware"} & set(changed + added):
        notes.append("обновились alsa-ucm-conf/sof-firmware: если пропал звук, снова запусти скрипт "
                     "chromebook-linux-audio")

    # .pacnew: only those this update made are its business; older ones wait, as they did before it
    made = [f for f in sorted({m.group(2) for m in PACNEW_MADE.finditer(pacman_log or "")})
            if _exists(f)]                      # merged meanwhile (manual mode): nothing to do
    waiting = pacnew_files()
    if made:
        warnings.append(("новый .pacnew от этого обновления: " if len(made) == 1 else
                         "новые .pacnew от этого обновления: ") + ", ".join(made[:6])
                        + (f" и ещё {len(made) - 6}" if len(made) > 6 else "")
                        + " — разберём вместе (pacdiff -o покажет список)")
    elif pacman_log is None and (waiting or "pacnew" in seen_notes):
        warnings.append("есть .pacnew файлы — разберём вместе (pacdiff -o покажет список)")
    elif waiting or "pacnew" in seen_notes:
        notes.append(f".pacnew от прошлых обновлений{f': {len(waiting)}' if waiting else ''} — "
                     "посмотреть: pacdiff -o")

    # Orphans: only those this update left behind are its business.
    orphans_now = orphans()
    if orphans_before is not None:
        new = sorted(orphans_now - set(orphans_before))
        if new:
            warnings.append(("новый пакет-сирота после обновления: " if len(new) == 1 else
                             "новые пакеты-сироты после обновления: ") + ", ".join(new[:8])
                            + (f" и ещё {len(new) - 8}" if len(new) > 8 else "")
                            + " — не удалял, посмотреть: pacman -Qdtq")
        elif orphans_now:
            notes.append(f"сироты: {len(orphans_now)} — посмотреть: pacman -Qdtq")
    elif orphans_now or "orphans" in seen_notes:
        warnings.append("есть пакеты-сироты — не удалял, посмотреть: pacman -Qdtq")

    failed = failed_units()
    services = services_record(responder, failed)
    if services["restarted"]:
        ok.append(restarted_text(services["restarted"]))
    if services["failed"]:
        warnings.append(not_restarted_text(services["failed"], "systemctl status " + " ".join(services["failed"][:3])))
    elif exitstatus == 11:
        warnings.append("arch-update не смог перезапустить сервисы (код 11) — посмотри: systemctl --failed")
    if services["waiting"]:
        warnings.append(waiting_text(services["waiting"]))
    if services["later"]:
        notes.append(later_text(services["later"]))
    if "services" in seen_notes and not services["listed"]:
        notes.append("arch-update предлагал перезапустить сервисы — они перезапустятся после перезагрузки")
    new_failed = [u for u in failed if u not in failed_before and u not in services["failed"]]
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
    for n in notes:
        LOG.event("ЗАМЕТКА", n)
    if pacman_log is not None:
        LOG.text("\n══════ /var/log/pacman.log за это обновление ══════\n" + pacman_log)

    if problems:
        notify("Обновление: нужна проверка", "\n".join(problems), urgent=True)
    else:
        clean = "Всё прошло чисто. Перезагрузись, когда будет удобно." if reboot else "Всё прошло чисто."
        notify("Обновление завершено", "\n".join(warnings) or clean)
    res = {"changed": changed, "added": added, "removed": removed, "problems": problems,
           "warnings": warnings, "notes": notes, "ok": ok, "verdict": 2 if problems else (1 if warnings else 0),
           "log": LOG.path, "exitstatus": exitstatus, "started": True, "services": services, "boot": boot,
           "reboot": reboot}
    res["fixes"] = fix_commands(res)
    save_summary(res, responder)
    return res


# ───────────────────────────── fixes ─────────────────────────────

def fix_commands(res):
    """What the engine knows to do about the problems with booting in a result (or a summary read back), in the
    order to run them: [{"key", "label", "argv"}]. Each one only where it fits a problem found:
      mount      an fstab boot partition that is not mounted
      kernel     a kernel file missing or empty: the kernel package again (its hooks copy the kernel, build
                 the initramfs and the DKMS modules, sign); pacman asks before it does anything
      dkms       a DKMS module not built for a kernel
      initramfs  an initramfs (or UKI) missing, empty or broken, mkinitcpio failed, or after a DKMS build
                 (the module may belong in the initramfs): mkinitcpio -P, as pacman's hook does
      sbctl      a boot file not signed for Secure Boot: signed and kept in sbctl's list
    [] when there is nothing to do this way. They ask for the password: run them in a terminal."""
    issues = [i for i in (res or {}).get("boot") or [] if isinstance(i, dict)]
    su = elevation_command()
    fixes = []
    for t in dict.fromkeys(i.get("target") for i in issues if i.get("kind") == "unmounted"):
        if isinstance(t, str) and t.startswith("/"):
            fixes.append({"key": "mount", "label": f"смонтировать {t}", "argv": [su, "mount", t]})
    pkgs = sorted({i.get("pkg") for i in issues if i.get("kind") == "kernel" and isinstance(i.get("pkg"), str)})
    if pkgs:
        fixes.append({"key": "kernel", "argv": [su, "pacman", "-S", *pkgs],
                      "label": f"переустановить ядро ({', '.join(pkgs)}): вернёт файл ядра и initramfs"})
    kvers = list(dict.fromkeys(k for i in issues if i.get("kind") == "dkms" for k in (i.get("kvers") or [])
                               if isinstance(k, str) and re.fullmatch(r"[\w.+~-]+", k)))
    nvidia = any(i.get("module") == "nvidia" for i in issues if i.get("kind") == "dkms")
    for kver in kvers:
        fixes.append({"key": "dkms", "argv": [su, "dkms", "autoinstall", "-k", kver],
                      "label": f"собрать {'драйвер NVIDIA' if nvidia else 'модули DKMS'} под ядро {kver}"})
    kinds = {i.get("kind") for i in issues}
    if kinds & {"image", "initcpio"} or (kvers and glob.glob(os.path.join(PRESET_DIR, "*.preset"))):
        fixes.append({"key": "initramfs", "label": "собрать initramfs заново (mkinitcpio -P)",
                      "argv": [su, "mkinitcpio", "-P"]})
    for f in dict.fromkeys(f for i in issues if i.get("kind") == "unsigned" for f in (i.get("files") or [])):
        if isinstance(f, str) and f.startswith("/"):
            fixes.append({"key": "sbctl", "label": f"подписать для Secure Boot: {f}",
                          "argv": [su, "sbctl", "sign", "-s", f]})
    return fixes


def services_command(names):
    """The command that restarts these services now (those safe to restart while the machine runs; the others
    are left out). [] when none is left."""
    dm, own = display_manager(), own_service()
    names = [n for n in dict.fromkeys(names or ()) if isinstance(n, str) and not keeps_running(n, dm, own)]
    return [elevation_command(), "systemctl", "restart", "--", *names] if names else []


def services_restarted(log, names, ok=True):
    """After services_command(names) ran (ok: it succeeded): that update's summary says so, its verdict
    follows. Returns the summary, None if there is none."""
    names = [n for n in names if isinstance(n, str)]

    def change(d):
        s = d.get("services") if isinstance(d.get("services"), dict) else {}
        waiting = [u for u in s.get("waiting") or [] if u not in names]
        if ok:
            s["restarted"] = list(dict.fromkeys((s.get("restarted") or []) + names))
            s["skipped"] = [u for u in s.get("skipped") or [] if u not in names]
        else:
            s["failed"] = sorted(set((s.get("failed") or []) + names))
        d["warnings"] = [w for w in d.get("warnings") or [] if not w.startswith(WAITING_TEXTS)]
        if waiting:
            d["warnings"].append(waiting_text(waiting))
        if not ok:
            d["warnings"].append(not_restarted_text(names, "systemctl --failed"))
        else:
            d["ok"] = [o for o in d.get("ok") or [] if not o.startswith(RESTARTED_TEXTS)] \
                + [restarted_text(s["restarted"])]
        s["waiting"] = waiting
        d["services"] = s
    data = _update_summary(log, change)
    journal_event(log, "СЕРВИСЫ", ("перезапущены: " if ok else "не перезапустились: ") + ", ".join(names))
    return data


def boot_probe_command(argv, out):
    """argv (a fix) followed, in the same terminal session, by a probe of the boot files into the file out:
    after `sudo …` asked for the password in that terminal, only that session can look into a closed /boot.
    Give out to recheck_boot afterwards. The exit status is argv's."""
    files, images = probe_targets()
    return probe_command(argv, out, files, images, secure_boot_on() and bool(shutil.which("sbctl")))


def fixes_command(fixes, out):
    """All the fixes as one command for one key: in order, in one terminal session (sudo asks once), stopping at
    the first that fails, then the probe into out for recheck_boot(log, probe=out). Run it in a pty: sudo asks
    for the password there and pacman asks before it installs. Exit status: 0 when every fix went through."""
    script = " && ".join(shlex.join(f["argv"]) for f in fixes if isinstance(f, dict) and f.get("argv"))
    return boot_probe_command(["bash", "-c", script or "true"], out)


def probe_targets():
    """The boot files of the installed kernels' presets, and the initramfs images among them the user cannot
    read: what a probe looks at."""
    files, images = [], []
    for pkg in sorted(kernel_packages()):
        for kind, f in preset_files(pkg):
            files.append(f)
            if kind == "image" and not os.access(f, os.R_OK):
                images.append(f)
    return list(dict.fromkeys(files)), list(dict.fromkeys(images))


def recheck_boot(log, probe=None):
    """The problems with booting of that update as they are now (after a fix): the same look as after the update
    (boot partitions, the kernels' boot files, DKMS, Secure Boot). What cannot be looked at now (a /boot closed to
    the user while sudo asks for a password) stays as it was found. The update's summary follows: its problems,
    verdict and fixes. probe: the file boot_probe_command wrote (or parse_probe's dict). Returns the problems
    with booting, [] when there are none."""
    data = read_json(summary_path(log))
    old = [i for i in data.get("boot") or [] if isinstance(i, dict) and isinstance(i.get("text"), str)]
    if isinstance(probe, str):
        probe = read_probe(probe)
    issues, _, _, _ = boot_state(probe if isinstance(probe, dict) else None, earlier=old,
                                 event=lambda kind, msg: journal_event(log, kind, msg))
    texts = [i["text"] for i in issues]
    gone = {i["text"] for i in old}

    def change(d):
        d["problems"] = [p for p in d.get("problems") or [] if p not in gone] + texts
        d.update(boot=issues, fixes=fix_commands({"boot": issues}), rechecked=now_iso())
    if data:
        _update_summary(log, change)
    journal_event(log, "ПЕРЕПРОВЕРКА", "; ".join(texts) or "проблем с загрузкой не нашёл")
    return texts


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
    demo=True: cmd is a simulation; nothing about the real system is prepared or guarded, no service is
    restarted, and the summary goes to the demo's own journal folder.
    Returns (exitstatus, result-dict-or-None, responder)."""
    global LOG, VIEW, RUNNING, RUN_MODE
    VIEW = view
    if not TERM_OK or terminal_hung_up(view):   # window closed before anything started: start nothing
        return None, None, None
    cmd = cmd or os.environ.get("ARCH_UPDATE_CMD", "arch-update")
    RUN_MODE = "manual" if manual else "auto"
    LOG = RunLog()
    # The user's standing choice: restart the services safe to restart when arch-update asks (auto mode only:
    # in a manual run the user answers that question too).
    restart = read_settings().get("restart_services") is True and not manual and not demo
    LOG.event("СТАРТ", f"{cmd}, режим: {'ручной' if manual else 'автоответы'}, "
                       f"новости: {'останавливаться' if stop_on_news else 'не останавливаться'}, "
                       f"вид: {type(view).__name__}" + (", сервисы: перезапускать" if restart else ""))
    try:
        pacman_log_offset = os.path.getsize(PACMAN_LOG)
    except OSError:
        pacman_log_offset = 0
    before = installed_versions()
    failed_before = failed_units()
    orphans_before = orphans()
    if terminal_hung_up(view):      # closed while we were preparing (pacman -Q can take a moment)
        LOG.event("ОКНО", "окно закрыто до запуска — обновление не начинаю")
        return None, None, None
    argv = shlex.split(cmd)
    sudo_once, probe_out = False, None
    if not demo:
        missing = unmounted_boot()
        if missing:
            why = f"{', '.join(missing)} не смонтирован — обновление не начинаю (ядро записалось бы не туда)"
            return None, not_started(why, 2, [{"kind": "unmounted", "target": t, "text": why} for t in missing]), None
        if elevation_command() == "sudo" and sudo_asks_password():
            # The password once, at the start (ASK_SUDO_FIRST). Afterwards, in the same session and so with
            # its ticket, the wrapper looks at the boot files as root (PROBE): from here a closed /boot, a
            # root-only initramfs and sbctl's verdict could not be seen.
            LOG.event("SUDO", "sudo спрашивает пароль: спрошу один раз в начале")
            sudo_once = True
            try:
                fd, probe_out = tempfile.mkstemp(prefix="arch-update-probe-")   # 0600: this user's only
                os.close(fd)
            except OSError:
                probe_out = os.devnull
            files, images = probe_targets()
            argv = probe_command(argv, probe_out, files, images, secure_boot_on() and bool(shutil.which("sbctl")),
                                 ask_first=True)
        if battery() and can_inhibit():
            # a laptop must not fall asleep in the middle of a pacman transaction
            argv = INHIBIT + ["--who=Arch-Update", "--why=обновление системы"] + argv
    cols, rows = dims or shutil.get_terminal_size((120, 40))
    child = pexpect.spawn(argv[0], argv[1:], env=child_env(), dimensions=(rows, cols), echo=True)
    RUNNING = True
    if on_spawn:
        on_spawn(child)
    responder = Responder(child, stop_on_news, manual, restart_services=restart)
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
    probe = None
    if probe_out and probe_out != os.devnull:
        probe = read_probe(probe_out)
        try:
            os.remove(probe_out)
        except OSError:
            pass
        if probe:
            LOG.event("ПРОВЕРКА", f"после обновления посмотрел с sudo: файлов загрузки {len(probe['files'])}, "
                                  f"initramfs {len(probe['images'])}"
                                  + (", подписи" if probe["sbctl"] is not None else ""))
    if sudo_once and status == 125:
        return status, not_started("пароль sudo не принят — обновление не начиналось, система не менялась", 1), \
            responder
    if child.exitstatus in NOTHING_DONE and installed_versions() == before:
        return status, not_started(NOTHING_DONE[child.exitstatus], 1), responder
    result = system_checks(before, status, responder, pacman_log_offset, failed_before, orphans_before=orphans_before,
                           probe=probe, carry=not demo) if summary else None
    return status, result, responder
