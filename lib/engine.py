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
import configparser
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
PACMAN_PROCEED = next(rx for label, rx, _, _ in RULES if label == "pacman proceed")

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
# A file pacman could not write while it put a package's files in place. libarchive says only "Write failed", and
# pacman (7.1) tells two cases apart by the error behind it: a full disk (ENOSPC) is an error, "could not extract
# <path> (Write failed)", and the transaction is aborted halfway; any other (an I/O error, a quota, a size limit) is
# only a warning, "warning given when extracting <path> (Write failed)": the file stays cut short and pacman ends as
# if all went well. Either comes once per file, so each counts as one marker (UNWRITTEN), whatever the path.
UNWRITTEN = {"could not extract": "could not extract … (Write failed)",
             "warning given when extracting": "warning given when extracting … (Write failed)"}
UNWRITTEN_LINE = re.compile(r"(?P<what>could not extract|warning given when extracting) (?P<path>[^\n]+?) "
                            r"\(Write failed\)", re.I)
# The package pacman was putting in place when that happened (a whole line, never one cut off by a read). It stays
# registered as installed, with its files half written: "update again" leaves it so (pacman 7.1: "there is nothing
# to do"), and a reinstall stops at its own files ("exists in filesystem") when the database lost their list too.
UNFINISHED = re.compile(r"problem occurred while (?:upgrading|installing) ([A-Za-z0-9@._+-]+)\n")
# A database entry pacman could not write (a full disk: "could not open file …/desc: No space left on device"): the
# package stays listed, but pacman cannot read it any more ("invalid or corrupted package" for every reinstall of
# it) until the entry is taken out first (pacman -Rdd --dbonly). "<name>-<version>-<release>", a whole line.
UNWRITTEN_ENTRY = re.compile(r"could not update database entry ([A-Za-z0-9@._+-]+?)-[^-\s]+-[^-\s]+\n")
PKG_NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+-]*")      # what a package name may be (and nothing a shell reads)
# What every problem that names half-written packages says, followed by the commands that mend them
REINSTALL = "поставь заново: "
# What went wrong, in the output. mkinitcpio goes on after a hook or a module failed and writes the image all the
# same ("errors were encountered during the build"), and one without a single module when the kernel's module list
# is missing (depmod failed: a full disk) — the Chromebook that did not boot any more.
FAILURE_MARKERS = re.compile(r"Initcpio image generation FAILED|error: command failed to execute correctly"
                             r"|errors were encountered during the build|No modules were added to the image"
                             r"|depmod: FATAL|No space left on device|not enough free disk space"
                             r"|An error has occurred during the update process"
                             r"|invalid or corrupted package \(PGP signature\)|is unknown trust|is marginal trust"
                             r"|You can do this later by running: #? ?dd if=\S+ of=\S+"
                             r"|" + UNWRITTEN_LINE.pattern, re.I)
KERNEL_FLASH = re.compile(r"dd if=\S+ of=\S+")   # depthcharge kernels (ARM Chromebooks): flash by hand
KEYRING_TROUBLE = re.compile(r"PGP signature|unknown trust|marginal trust", re.I)
# The markers that say mkinitcpio built a broken or incomplete image (or none), and what the summary says of each.
INITCPIO_SAYS = {"initcpio image generation failed": "mkinitcpio не собрал initramfs",
                 "errors were encountered during the build": "mkinitcpio собрал initramfs с ошибками — он может быть "
                                                             "неполным",
                 "no modules were added to the image": "в initramfs не попал ни один модуль ядра — система с ним "
                                                       "не загрузится"}
# The disk ran full during the update ("No space left on device" in the output, pacman could not write a file with /
# left with next to no room), or a failed update left / with next to no room (FULL_LEFT): what is true of all of them,
# also after F made room. lib/fixes/space.py and the UI know it by this very text (the UI's "обнови ещё раз" too).
DISK_FULL = ("во время обновления на диске почти не осталось места — освободи его (например: sudo pacman -Sc) и "
             "обнови ещё раз")
# The same, as the summaries of 6201a1e…8f4397c (the version on the machines) say it: read back as DISK_FULL
DISK_FULL_OLD = ("на диске кончилось место («No space left on device») — часть файлов не записалась: освободи место "
                 "(например: sudo pacman -Sc) и обнови ещё раз")
FULL_LEFT = 64                   # MiB free on / below which a failed update counts as one that ran out of room
# The update's room on the disk, looked at when pacman asks before it changes anything. On btrfs the snapshots of /
# keep every file the update replaces, the one a hook takes at the start of the transaction (snap-pac,
# timeshift-autosnap) as well as those that are there already (snapper's timeline, Timeshift's schedule, earlier
# pairs): the transaction needs room for every new file, not only for the difference pacman counts ("Net Upgrade
# Size"). The Chromebook ran out of space in the middle of one that way (7 MiB counted, about 2 GiB needed).
SIZE_LINE = re.compile(r"^(Total Installed|Total Download|Net Upgrade) Size:\s+(-?[\d.]+)\s+(B|KiB|MiB|GiB|TiB)\s*$",
                       re.M)
SIZE_UNITS = {"B": 1, "KiB": 2 ** 10, "MiB": 2 ** 20, "GiB": 2 ** 30, "TiB": 2 ** 40}
SIZE_KEYS = {"Total Installed": "installed", "Total Download": "download", "Net Upgrade": "net"}
# pacman's own space check (CheckSpace), when it refuses: in blocks of that file system (statvfs f_bsize), the cushion
# it keeps included. It checks twice: the downloads' room before it downloads, the transaction's after (then it says
# "checking available disk space" first), each time for every file system concerned (a whole line, never one cut).
PARTITION_FULL = re.compile(r"Partition (/[^\n]*?) too full: (\d+) blocks needed, (\d+) blocks free\n")
HOOK_DIRS = ("/usr/share/libalpm/hooks", "/etc/pacman.d/hooks")    # the second overrides the first by name
# A hook's Exec that takes a snapshot: snap-pac's "snap-pac pre" (not its removal hook, which only deletes its
# notes in /tmp), timeshift-autosnap, a hand-made snapper create or btrfs subvolume snapshot.
SNAPSHOT_EXEC = re.compile(r"(?mi)^\s*Exec\s*=(?P<cmd>[^\n]*)$")
SNAPSHOT_CMD = re.compile(r"\bsnap-pac\s+pre\b|\btimeshift-autosnap\b|\bsnapper\b[^\n]*\bcreate\b"
                          r"|\bbtrfs\s+subvolume\s+snapshot\b")
SNAPPER_ROOT = "/etc/snapper/configs/root"   # snap-pac snapshots the snapper config "root" (/) unless told not to
SNAP_PAC_INI = "/etc/snap-pac.ini"
# Snapshots of / that are there already, as a user can see them: snapper's list of configs (world-readable, shell
# syntax: SNAPPER_CONFIGS="root home"; archinstall's "Snapper" makes "root" with timeline snapshots and no snap-pac),
# the folder snapper (and others) keep the snapshots of / in, Timeshift's settings in BTRFS mode.
SNAPPER_CONF = "/etc/conf.d/snapper"
SNAPSHOTS_DIR = "/.snapshots"
TIMESHIFT_CONF = "/etc/timeshift/timeshift.json"     # "btrfs_mode" : "true" (Timeshift writes every value as text)
SNAPSHOT_TOOLS = {"snapper": "snapper", "timeshift": "Timeshift"}     # by name in the texts; another: "снимки системы"
SPACE_MARGIN = 256 * 2 ** 20     # on top: the hooks' own files (initramfs, caches), btrfs's metadata
MOUNTINFO = "/proc/self/mountinfo"
SPACE_HEAD = "мало места на диске"
FULL_BEFORE = ("pacman не начал: мало места на диске («not enough free disk space») — обновление не начиналось, "
               "система не менялась")
# mkinitcpio's "the image was built" (v38 on: "Initcpio image generation successful"; v37: "Image generation
# successful"), and what says it was not, or not whole.
IMAGE_BUILT = re.compile(r"(?i)image generation successful")
IMAGE_TROUBLE = re.compile(r"(?i)image generation FAILED|errors were encountered during the build"
                           r"|No modules were added to the image")
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
    r"NetworkManager", r"wpa_supplicant(?:-nl80211|-wired)?(?:@.*)?", r"iwd", r"systemd-networkd", r"dhcpcd(?:@.*)?",
    r"connman", r"netctl(?:-auto|-ifplugd)?@.*", r"systemd-resolved", r"ModemManager", r"tailscaled", r"zerotier-one",
    r"openvpn.*", r"strongswan.*", r"wg-quick@.*", r"mullvad-daemon", r"nordvpnd", r"warp-svc", r"netbird",
    # the keyboard and the mouse in use (Bluetooth, key remappers like keyd on Chromebooks), a shared desktop
    r"bluetooth", r"keyd", r"kanata.*", r"kmonad.*", r"input-remapper.*", r"udevmon", r"xremap.*",
    r"xrdp(?:-sesman)?", r"gnome-remote-desktop", r"teamviewerd", r"anydesk", r"rustdesk", r"x0vncserver.*",
    r"vncserver.*", r"x11vnc.*",
    # NVIDIA's daemons: the new driver works only with its new kernel module, after a reboot
    r"nvidia.*",
    # containers and machines go down with them, and not all of them come back by themselves; the virtual
    # machines' logs and locks
    r"docker", r"systemd-nspawn@.*", r"libvirt-guests", r"virtlogd", r"virtlockd",
    # they run package transactions of their own: a restart in the middle of one would cut pacman off
    r"packagekit", r"pamac-daemon",
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
PROC_STAT = "/proc/stat"                              # its btime: when this boot began
CGROUP = "/proc/self/cgroup"
PACMAN_CACHE = "/var/cache/pacman/pkg"
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


def run(cmd, timeout=60, env=None, merge=False):
    """What a command printed (merge: its errors too, in order), "" when it could not run or took too long."""
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, timeout=timeout, env=env).stdout
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


def boot_started():
    """When this boot began, in seconds since the epoch (the kernel's btime); None if that cannot be read."""
    try:
        with open(PROC_STAT) as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def free_mib(path="/"):
    """Free space where path is, in MiB for the user (what the update's own files get); None if unknown."""
    try:
        return shutil.disk_usage(path).free // 2 ** 20
    except OSError:
        return None


def pacman_busy():
    return os.path.exists(PACMAN_LOCK) and bool(run(["pgrep", "-x", "pacman"]).strip())


def snapshot_hooks():
    """The pacman hooks that take a snapshot of / at the start of a transaction (snap-pac's pre hook while snapper
    has its "root" config, timeshift-autosnap, a hand-made snapper create or btrfs subvolume snapshot), by name. One
    in /etc/pacman.d/hooks overrides the same name in /usr/share/libalpm/hooks (a link to /dev/null switches it
    off). Not all there is to it: snapshots taken before (snapper's timeline, Timeshift's schedule) hold the old
    files too, with no hook at all (root_snapshots)."""
    return [name for name, _ in _snapshot_hooks()]


def _snapshot_hooks():
    """snapshot_hooks, each with the tool that takes the snapshot: [(name, "snapper" | "timeshift" | "btrfs")]."""
    hooks = {}
    for d in HOOK_DIRS:
        for path in glob.glob(os.path.join(d, "*.hook")):
            hooks[os.path.basename(path)] = path
    found = []
    for name, path in sorted(hooks.items()):
        try:
            if os.path.realpath(path) == os.devnull:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(65536)
        except OSError:
            continue
        if not re.search(r"(?mi)^\s*When\s*=\s*PreTransaction\b", text):
            continue
        cmds = [m.group("cmd") for m in SNAPSHOT_EXEC.finditer(text)]
        took = [m.group(0) for c in cmds for m in [SNAPSHOT_CMD.search(c)] if m]
        if not took:
            continue
        if any(re.search(r"\bsnap-pac\s+pre\b", c) for c in cmds) and not snap_pac_takes_root():
            continue
        tool = "timeshift" if "timeshift" in took[0] else "btrfs" if took[0].startswith("btrfs") else "snapper"
        found.append((name, tool))
    return found


def snapper_configs():
    """The snapper configs /etc/conf.d/snapper lists (SNAPPER_CONFIGS, shell syntax; the last one counts, as when
    the shell reads it). [] without any, or without snapper."""
    try:
        with open(SNAPPER_CONF, encoding="utf-8", errors="replace") as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?SNAPPER_CONFIGS=(.*)$", line)
        if m:
            try:
                names = " ".join(shlex.split(m.group(1), comments=True)).split()
            except ValueError:                  # an unclosed quote: the words as they are
                names = m.group(1).replace('"', " ").replace("'", " ").split()
    return names


def timeshift_btrfs():
    """Timeshift is set up in BTRFS mode: its snapshots are subvolume snapshots of / (and keep what they hold)."""
    mode = read_json(TIMESHIFT_CONF).get("btrfs_mode")
    return mode is True or (isinstance(mode, str) and mode.strip().lower() == "true")


def root_snapshots():
    """Which tool keeps snapshots of / on this machine, as a user can tell: "snapper", "timeshift", "btrfs" (another,
    or one by hand); None when there is none to see, or / is not btrfs. Every file an update replaces stays in the
    newest snapshot (on btrfs it shares the old file's blocks), so such an update needs room for all its new files:
    a hook that takes one at the start of the transaction (snapshot_hooks), snapper's "root" config (its timeline
    snapshots, with or without snap-pac), Timeshift in BTRFS mode, or snapshots in /.snapshots (one that is empty,
    as far as a user may look into it, holds nothing)."""
    if mount_of("/")[0] != "btrfs":
        return None
    hooks = _snapshot_hooks()
    if hooks:
        return hooks[0][1]
    configs = snapper_configs()
    if "root" in configs:
        return "snapper"
    if timeshift_btrfs():
        return "timeshift"
    if os.path.isdir(SNAPSHOTS_DIR):
        try:
            held = bool(os.listdir(SNAPSHOTS_DIR))
        except OSError:                         # snapper's own is root's only (0750): there is something to keep
            held = True
        if held:
            return "snapper" if configs else "btrfs"
    return None


def snap_pac_takes_root():
    """snap-pac snapshots / : snapper has its config "root", and /etc/snap-pac.ini does not switch it off (its
    "snapshot" in [root] or [DEFAULT], true by default for root)."""
    if not os.path.exists(SNAPPER_ROOT):
        return False
    ini = configparser.ConfigParser()
    try:
        ini.read(SNAP_PAC_INI, encoding="utf-8")
        return ini.getboolean("root", "snapshot", fallback=ini.getboolean("DEFAULT", "snapshot", fallback=True))
    except (configparser.Error, ValueError, OSError):
        return True                             # cannot tell: as snap-pac would by default


def mount_of(path):
    """(file system type, source) of the mount that holds path (/proc/self/mountinfo); (None, None) if unknown.
    Two subvolumes of one btrfs have one source: they share its free space."""
    best, info = "", (None, None)
    try:
        real = os.path.realpath(path)
        with open(MOUNTINFO, encoding="utf-8", errors="replace") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                parts, rest = left.split(), right.split()
                if len(parts) < 5 or len(rest) < 2:
                    continue
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[4])
                if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
                    best, info = point, (rest[0], rest[1])
    except OSError:
        pass
    return info


def pacman_sizes(text):
    """{"installed": bytes, "download": bytes, "net": bytes (Net Upgrade Size, below 0 when the update makes room)}
    from the totals pacman printed before its question (the last list in text; download and net only when pacman
    printed them), None without them."""
    start = max(text.rfind("\nPackages ("), text.rfind("\nPackage ("))
    sizes = {}
    for kind, number, unit in SIZE_LINE.findall(text[start:] if start >= 0 else text):
        try:
            sizes[SIZE_KEYS[kind]] = int(float(number) * SIZE_UNITS[unit])
        except ValueError:
            pass
    return sizes if "installed" in sizes else None


def size_text(n):
    """Bytes for the user: "1,4 ГБ", "830 МБ"."""
    return f"{n / 2 ** 30:.1f} ГБ".replace(".", ",") if n >= 2 ** 30 else f"{max(0, n) // 2 ** 20} МБ"


def snapshots_text(tool):
    """Who keeps the old files: "снимки snapper", "снимки Timeshift", else "снимки системы"."""
    return f"снимки {SNAPSHOT_TOOLS[tool]}" if isinstance(tool, str) and tool in SNAPSHOT_TOOLS else "снимки системы"


def space_facts(space):
    """"свободно 1,2 ГБ, а обновлению нужно около 2,2 ГБ (не хватает 1,0 ГБ; снимки snapper хранят …)" for what
    Responder.space_short found. The shortfall always: free and need can round to the same "1,2 ГБ"."""
    def number(key):
        v = space.get(key)
        return v if type(v) is int else 0
    free, need = number("free"), number("need")
    short = -(-max(need - free, 1) // 2 ** 20) * 2 ** 20          # up to whole MiB: never "не хватает 0 МБ"
    why = (f"; {snapshots_text(space.get('tool'))} хранят старые версии файлов, пока ставятся новые"
           if space.get("snapshots") else "")
    return (f"свободно {size_text(free)}, а обновлению нужно около {size_text(need)} (не хватает "
            f"{size_text(short)}{why})")


def space_text(space):
    """Why an update was not begun for want of room, what matters first (the summary may show only its start)."""
    return f"{SPACE_HEAD} — обновление не начинал, система не менялась: {space_facts(space)}"


def pacman_refusal(responder):
    """What pacman's own space check refused (Responder.refused), as the summary of an update that did not begin
    says it: (text, space) — space {"need", "free"} for the space fix when / was too full, else None (another file
    system: freeing / does not help it). need: what pacman said it needs on / (its blocks, its cushion included);
    when it refused after the download, those downloads again (the cache on the same file system: the space fix's
    pacman -Sc removes packages not installed, and the next update downloads them again); when it refused the
    download itself, the update's own growth after it (Net Upgrade Size, else Total Installed Size); and SPACE_MARGIN
    for the hooks. None without pacman's line (it said only "not enough free disk space")."""
    refused = getattr(responder, "refused", None)
    if not isinstance(refused, dict):
        return None
    mount, needed, free_blocks = refused.get("mount"), refused.get("needed"), refused.get("free")
    try:
        bsize = os.statvfs(mount).f_bsize      # pacman's block (it counts in f_bsize)
    except (OSError, TypeError, ValueError):
        return None
    free = free_blocks * bsize                  # as pacman counted it, beside what it needed
    if mount != "/":
        short = max(needed - free_blocks, 1) * bsize
        return (f"{SPACE_HEAD} на {mount} — pacman не начал обновление, система не менялась: не хватает "
                f"{size_text(-(-short // 2 ** 20) * 2 ** 20)}"), None
    sizes = getattr(responder, "sizes", None) or {}
    need = needed * bsize
    if refused.get("installing"):
        root, cache = mount_of("/"), mount_of(PACMAN_CACHE)
        need += sizes.get("download", 0) if cache[1] is not None and cache == root else 0
    else:
        need += max(sizes.get("net", sizes.get("installed", 0)), 0)
    space = {"need": need + SPACE_MARGIN, "free": free}
    return f"{SPACE_HEAD} — pacman не начал обновление, система не менялась: {space_facts(space)}", space


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


def module_index(kver):
    """The module list depmod writes for a kernel: modprobe, udev and mkinitcpio find the kernel's modules by it."""
    return os.path.join(MODULES_DIR, kver, "modules.dep.bin")


def modular(kver):
    """The kernel has modules to load (modules.order lists them, as every Arch kernel's does): it boots only with
    its module list, and with an initramfs that holds some of them."""
    try:
        return os.path.getsize(os.path.join(MODULES_DIR, kver, "modules.order")) > 0
    except OSError:
        return False


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
#   indexed <kver,kver|-> <path> (the kernels whose module list, modules.dep.bin, is in that image: it has modules),
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
    d=$(printf '%s\n' "$l" | sed -n 's|^usr/lib/modules/\([^/][^/]*\)/modules\.dep\(\.bin\)\{0,1\}$|\1|p' \
        | sort -u | paste -sd, -)
    printf 'image %s %s %s\n' "$rc" "${k:--}" "$f"
    printf 'indexed %s %s\n' "${d:--}" "$f"
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
    inside})}, "indexed": {path: {kernel versions whose module list is inside}}, "sbctl": its output, None if not
    asked}."""
    lines = (text or "").splitlines()
    if lines[:1] != ["sudo-ok"]:
        return None
    probe, sb = {"files": set(), "sizes": {}, "images": {}, "indexed": {}, "sbctl": None}, None
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
        elif kind == "indexed":
            kvers, _, path = rest.partition(" ")
            if path:
                probe["indexed"][path] = set(kvers.split(",")) - {"-", ""}
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


def owners(paths):
    """The packages that own these files (pacman -Qo), each once, in their order; [] when none is known."""
    found = []
    for line in run(["pacman", "-Qo", "--", *paths], env=child_env()).splitlines() if paths else ():
        m = re.match(r"^\S.* is owned by ([A-Za-z0-9@._+-]+) \S+$", line)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def aur_helper():
    """The AUR helper arch-update uses: its AURHelper setting, else yay or paru, whichever is installed (yay first, as
    arch-update looks); None without one."""
    conf = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                        "arch-update", "arch-update.conf")
    try:
        with open(conf) as f:
            for line in f:
                m = re.match(r"\s*AURHelper\s*=\s*(\S+)", line)
                if m and m.group(1) in ("yay", "paru"):
                    return m.group(1)
    except OSError:
        pass
    return next((h for h in ("yay", "paru") if shutil.which(h)), None)


def helper_package(pkg, version):
    """The file of that package version an AUR helper built and keeps (yay: ~/.cache/yay/<package base>/, paru:
    ~/.cache/paru/clone/<package base>/, both under XDG_CACHE_HOME when set), None when there is none."""
    cache = glob.escape(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"))
    want = re.compile(re.escape(f"{pkg}-{version}-") + PKG_ARCH + r"\.pkg\.tar(?:\.\w+)?")
    found = [p for d in (os.path.join(cache, "yay", "*"), os.path.join(cache, "paru", "clone", "*"))
             for p in glob.glob(os.path.join(d, glob.escape(pkg) + "-*.pkg.tar*"))
             if want.fullmatch(os.path.basename(p))]
    return max(found, key=_mtime) if found else None


def sync_version(pkg):
    """The version of a package the repositories have (pacman -Si: the first repository that has it), None when none
    has it. Read from the sync database even for an entry pacman can no longer read (pacman -Qu leaves that out)."""
    m = re.search(r"^Version\s*:\s*(\S+)", run(["pacman", "-Si", "--", pkg], timeout=20, env=child_env()), re.M)
    return m.group(1) if m else None


def shown_path(path):
    """A path as a command line for the user shows it: in their home folder from ~ (the shell expands it before
    sudo), quoted where the shell would read it otherwise."""
    home = os.path.expanduser("~").rstrip("/")
    if home and path.startswith(home + "/"):
        return "~/" + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def reinstall_commands(names, dbonly=True, broken=(), installed=None):
    """The commands that put half-written packages whole again, from the package file of the installed version
    where there is one: [(names, command)], one command for each way of doing it (a pacman line fails as a whole,
    so packages mended one way never share one with those mended another):
      the file of the installed version, in pacman's cache (pacman -Sc keeps it) or where yay or paru built it:
          sudo pacman -Udd --dbonly F… && sudo pacman -U F…
      a repository's package without a file, the repositories at that same version (pacman -Si):
          sudo pacman -Sdd --dbonly X… && sudo pacman -S X…
      a repository's package without a file, the repositories at another version: that one, with the whole update
      (alone it would be a partial upgrade; "update again" alone leaves the package as it is):
          sudo pacman -Sdd --dbonly X… && sudo pacman -Syu X…
      a package from the AUR (pacman -Qqm) without a file: built again by the AUR helper, which passes --dbonly on:
          yay -Sdd --dbonly X… && yay -S X…
    dbonly: pacman's database may have lost their file lists as well (it ran out of room too): written again first
    (--dbonly touches no file), so that their own files are theirs again ("exists in filesystem" otherwise); with
    -dd, so that it writes these entries only (a dependency it pulled in would be registered without its files);
    then the ordinary reinstall. broken: packages whose entry pacman could not write at all (it cannot read it any
    more): taken out first (pacman -Rdd --dbonly, which leaves their files). Never --overwrite '*': it takes other
    packages' files as well. Each verified on pacman 7.1 (yay 13, paru 2): a file list lost, files missing, files
    zeros at full size, an entry pacman cannot read. A package neither installed nor in a repository is left out.
    installed: pacman -Q as a dict, if known."""
    names = [n for n in dict.fromkeys(names or ()) if isinstance(n, str) and PKG_NAME.fullmatch(n)]
    if not names:
        return []
    installed = installed_versions() if installed is None else installed
    foreign = set(run(["pacman", "-Qqm"], env=child_env()).split())
    ways = {}                                   # way -> [(name, file)]
    for n in names:
        ver = installed.get(n)                  # (none: pacman could not even make its entry)
        f = (cached_package(n, ver) or helper_package(n, ver)) if ver else None
        sync = None if f or n in foreign else sync_version(n)
        if not (f or n in foreign or sync or ver):
            continue                            # neither installed nor anywhere to install it from
        way = "file" if f else "aur" if n in foreign else "repo" if sync in (ver, None) else "newer"
        ways.setdefault(way, []).append((n, f))
    helper = aur_helper()
    out = []
    for way in ("file", "repo", "newer", "aur"):
        if way not in ways:
            continue
        pkgs = [n for n, _ in ways[way]]
        gone = [n for n in pkgs if n in set(broken or ())]
        pre = f"sudo pacman -Rdd --dbonly {' '.join(gone)} && " if gone and dbonly else ""
        what = " ".join(shown_path(f) for _, f in ways[way]) if way == "file" else " ".join(pkgs)
        if way == "file":
            cmd = f"sudo pacman -Udd --dbonly {what} && sudo pacman -U {what}" if dbonly else f"sudo pacman -U {what}"
        elif way == "aur" and helper:
            cmd = f"{helper} -Sdd --dbonly {what} && {helper} -S {what}" if dbonly else f"{helper} -S {what}"
        elif way == "aur":
            cmd = f"собери {what} заново из AUR (makepkg) и поставь файл пакета: sudo pacman -Udd --dbonly <файл> " \
                  f"&& sudo pacman -U <файл>" if dbonly else f"собери {what} заново из AUR (makepkg) и поставь"
        else:
            pacman_s = "-Syu" if way == "newer" else "-S"
            cmd = f"sudo pacman -Sdd --dbonly {what} && sudo pacman {pacman_s} {what}" if dbonly else \
                f"sudo pacman {pacman_s} {what}"
        out.append((pkgs, pre + cmd))
    return out


# pacman -Qk / -Qkk (pacman 7.1, English: child_env): a total per package, a warning per file that is not as packaged
CHECK_TOTAL = re.compile(r"^(?P<pkg>\S+): (?P<total>\d+) total files?, \d+ (?:missing|altered) files?$", re.M)
CHECK_FILE = re.compile(r"^warning: (?P<pkg>\S+): (?P<path>/[^\n]*) \((?P<why>[^()\n]*)\)$", re.M)
CONTENT_WRONG = ("Size mismatch", "MD5 checksum mismatch", "SHA256 checksum mismatch")


def package_state(pkg, as_root=False):
    """How a package pacman could not finish is now, by pacman's own look (it only reads): None when it is not
    installed any more, else {"whole": bool, "entry": bool (pacman can read its database entry: else it is taken out
    before a reinstall, see reinstall_commands)}; not whole either when pacman did not answer. Not whole:
      its file list lost from pacman's database: pacman -Qk counts 0 files where the package's own list of them (its
          mtree, pacman -Qkk) has some, or has none to tell
      a file missing: pacman -Qk's "No such file or directory" (it leaves out what NoExtract keeps out)
      a file not as packaged: pacman -Qkk's size or checksum mismatch (a full disk leaves files cut short, or zeros
          at their full size, which only the checksum tells; also where another file system ran full, with pacman's
          database whole)
    Not counted, as on a machine where nothing went wrong (pacman 7.1 on a stock Arch said these of 179 packages):
    what this user may not read (a closed /boot, /etc/shadow: "Permission denied", "failed to calculate SHA256
    checksum"), a configuration file changed since ("backup file: …"), permissions, owners and times (a mount point,
    a folder made by hand), and -Qkk's own "No such file or directory" (it does not leave out folders NoExtract
    emptied). as_root: through sudo -n (when it asks nothing): then there is nothing it may not read."""
    def look(*opts):
        cmd = ["pacman", *opts, "--", pkg]
        out = run(["sudo", "-n", *cmd], timeout=900, env=child_env(), merge=True) if as_root else ""
        if not CHECK_TOTAL.search(out):         # no sudo after all (it asks, or may not run pacman): as this user
            out = run(cmd, timeout=900, env=child_env(), merge=True)
        return out
    quick = look("-Qk")
    total = next((int(m.group("total")) for m in CHECK_TOTAL.finditer(quick) if m.group("pkg") == pkg), None)
    if total is None and re.search(r"package '" + re.escape(pkg) + r"' was not found", quick):
        return None                             # removed meanwhile: nothing left to mend
    entry = not re.search(r"could not open file \S*/" + re.escape(pkg) + r"-[^/\s]+/(?:desc|files)\b", quick)
    if total is None or any(m.group("pkg") == pkg and m.group("why") == "No such file or directory"
                            for m in CHECK_FILE.finditer(quick)):
        return {"whole": False, "entry": entry}     # (no answer at all: as it was)
    full = look("-Qkk")
    listed = next((int(m.group("total")) for m in CHECK_TOTAL.finditer(full) if m.group("pkg") == pkg), None)
    if listed is None or (total == 0 and listed != 0):
        return {"whole": False, "entry": entry}     # no file list where the package has files, or nothing to tell
    wrong = [m for m in CHECK_FILE.finditer(full) if m.group("pkg") == pkg and m.group("why") in CONTENT_WRONG]
    return {"whole": entry and not wrong, "entry": entry}


def reinstall_text(names, dbonly=True, broken=(), installed=None):
    """reinstall_commands as one line (each way after the one before: "; "), "" when nothing is installed."""
    return "; ".join(cmd for _, cmd in reinstall_commands(names, dbonly, broken, installed))


def unfinished_lines(names, dbonly=True, broken=(), full=False, since=None, installed=None):
    """The problems that name half-written packages, one for each way of mending them (reinstall_commands):
    "пакет X записан не целиком — [когда освободишь место, ]поставь заново: …", an earlier update's (since: when it
    began, "" unknown) as "не исправлено после обновления в …: пакет X записан не целиком — поставь заново: …"."""
    lines = []
    for pkgs, cmd in reinstall_commands(names, dbonly, broken, installed):
        one = len(pkgs) == 1
        said = f"{'пакет' if one else 'пакеты'} {', '.join(pkgs)} {'записан' if one else 'записаны'} не целиком — " \
            + ("когда освободишь место, " if full else "") + REINSTALL + cmd
        if since is not None:
            said = (f"не исправлено после обновления в {since}: " if since else
                    "не исправлено после прошлого обновления: ") + said
        lines.append(said)
    return lines


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
    def __init__(self, child, stop_on_news, manual, restart_services=False, check_space=False):
        self.child = child
        self.stop_on_news = stop_on_news
        # check_space: before pacman changes anything, is there room for it (space_short); never in the demo
        self.check_space = check_space
        self.space = None         # what space_short found: the update stopped (or, in a manual run, was warned)
        self._snapshots = None
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
        self.unwritten = {}       # UNWRITTEN marker -> the first files it was said of (system_checks names one)
        self.unfinished = []      # the packages pacman could not finish putting in place (UNFINISHED)
        self.unwritten_entries = []   # the packages whose database entry pacman could not write (UNWRITTEN_ENTRY)
        self.sizes = None         # pacman's totals at its last "Proceed with installation?" (pacman_sizes)
        self.refused = None       # pacman's own space check refused: {"mount", "needed", "free" (blocks), "installing"}
        self._checking = False    # pacman checks the transaction's room now (after the download)
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
            marker = m.group(0)
            said = UNWRITTEN_LINE.fullmatch(marker)
            if said:                            # one marker for all the files it is said of
                marker = UNWRITTEN[said.group("what").lower()]
                files = self.unwritten.setdefault(marker, [])
                if len(files) < 20 and said.group("path") not in files:
                    files.append(said.group("path"))
            self.failures.add(marker)
        if "problem occurred while" in recent:
            for m in UNFINISHED.finditer(recent):
                if m.group(1) not in self.unfinished and len(self.unfinished) < 20:
                    self.unfinished.append(m.group(1))
        if "could not update database entry" in recent:
            for m in UNWRITTEN_ENTRY.finditer(recent):
                if m.group(1) not in self.unwritten_entries and len(self.unwritten_entries) < 20:
                    self.unwritten_entries.append(m.group(1))
        if "restart" in recent:
            self.watch_services(recent)
        question = "Proceed with" in recent and self.waiting() and PACMAN_PROCEED.search(self.current_line())
        if question:                            # a transaction's totals, before its downloads scroll them away
            self.sizes = pacman_sizes(self.tail) or self.sizes
            self._checking = False
        if "checking available disk space" in recent:
            self._checking = True
        if "too full:" in recent:
            for m in PARTITION_FULL.finditer(recent):
                if self.refused is None or m.group(1) == "/":       # / first: the space fix makes room there
                    self.refused = {"mount": m.group(1), "needed": int(m.group(2)), "free": int(m.group(3)),
                                    "installing": self._checking}
        if self.mode == "manual" and self.space is None and question:
            short = self.space_short()
            if short:                           # the user answers in a manual run: tell them before they do
                self.space = dict(short, stopped=False)
                LOG.event("МЕСТО", f"{SPACE_HEAD}: {space_facts(short)} — ручной режим: предупредил")
                say(f"\n[автоответчик] ВНИМАНИЕ: {SPACE_HEAD}: {space_facts(short)}. Ответь n, освободи место "
                    "(F в итоге) и обнови снова.\n", C_RED)
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

    def space_short(self):
        """At pacman's question before it changes anything: when snapshots keep the files the update replaces (a
        snapshot of / there already or taken at the start of the transaction: root_snapshots, on btrfs), is there
        room for every new file, and for the downloads when they go to the same file system? None when there is, or
        no telling (no snapshots: pacman's own count holds); else {"need", "free", "installed", "download" (bytes),
        "snapshots": True, "tool": the one that keeps them (root_snapshots)}."""
        if not self.check_space:
            return None
        if self._snapshots is None:
            self._snapshots = root_snapshots() or ""
        if not self._snapshots:
            return None
        sizes = pacman_sizes(self.tail)
        if not sizes:
            return None
        root, cache = mount_of("/"), mount_of(PACMAN_CACHE)
        download = sizes.get("download", 0) if cache[1] is not None and cache == root else 0
        need = sizes["installed"] + download + max(SPACE_MARGIN, sizes["installed"] // 10)
        try:
            free = shutil.disk_usage("/").free
        except OSError:
            return None
        if free >= need:
            return None
        return {"need": need, "free": free, "installed": sizes["installed"], "download": download, "snapshots": True,
                "tool": self._snapshots}

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
                if label == "pacman proceed" and not self.safe_only:
                    short = self.space_short()
                    if short:
                        self.space = dict(short, stopped=True)
                        LOG.event("МЕСТО", space_text(short))
                        return f"{SPACE_HEAD}: {space_facts(short)} — не начинаю", NO, "space"
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
#   space (too little room: Responder.space_short, pacman_refusal), unfinished [package] (left half written, by this
#   update or an earlier one, and not whole yet: every later update looks at them again, see unfinished_before),
#   later on: rechecked (recheck_boot), resolved (mark_resolved).
SUMMARY_KEYS = ("verdict", "changed", "added", "removed", "problems", "warnings", "notes", "ok", "exitstatus",
                "started", "head", "services", "fixes", "boot", "reboot", "space", "unfinished")


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
    UNSAVED[summary_path(log)] = data
    save_pending()


# Summaries a full disk did not let be written (the Chromebook: the state folder on the same disk): kept, and written at
# the first chance there is room (save_pending), so that what they say (a disk that ran full, packages left half
# written) reaches the menu's warning and the next update's look. Only while this program runs.
UNSAVED = {}


def save_pending():
    """Write the summaries kept in UNSAVED, those there is room for now. Never raises."""
    for path, data in list(UNSAVED.items()):
        try:
            write_json(path, data)
        except OSError:
            continue                            # still no room: at the next chance
        except Exception:
            pass                                # it never would be written: not tried again
        UNSAVED.pop(path, None)


def last_update(logdir=None):
    """The newest update's summary, with "log": its journal; None before the first one."""
    return next(summaries(logdir), None)


def summaries(logdir=None):
    """The summaries of the updates in the journal folder, newest first, each with "log": its journal."""
    for path in sorted(glob.glob(os.path.join(logdir or logs_dir(), "20*.json")), reverse=True):
        data = read_json(path)
        if data:
            data = normal_summary(data)
            data["log"] = path[:-len(".json")] + ".log"
            yield data


def normal_summary(d):
    """A summary as the code that reads it expects it, whatever wrote it (another version of the updater, a hand):
    lists of text, the problems with booting and the fixes as dicts, the services as lists of names. What is of
    another type is left out."""
    def plain(v):
        if isinstance(v, list):
            return [x for x in v if isinstance(x, str)]
        return v if isinstance(v, (str, int, float, bool)) or v is None else None

    def item(i):                                # lists where lists belong (kvers, files, argv), else a plain value
        return {k: plain(v) for k, v in i.items()
                if plain(v) is not None and isinstance(v, list) == (k in ("kvers", "files", "argv", "args"))}
    for k in ("changed", "added", "removed", "problems", "warnings", "notes", "ok"):
        d[k] = plain(d.get(k)) if isinstance(d.get(k), list) else []
    # a full disk as the previous version said it: as this one says it (F knows it by DISK_FULL)
    d["problems"] = [DISK_FULL if p == DISK_FULL_OLD else p for p in d["problems"]]
    for k in ("boot", "fixes"):
        items = d.get(k) if isinstance(d.get(k), list) else []
        d[k] = [item(i) for i in items if isinstance(i, dict)]
    d["fixes"] = [f for f in d["fixes"] if f.get("argv")]
    svc = d.get("services") if isinstance(d.get("services"), dict) else {}
    d["services"] = {k: plain(v) for k, v in svc.items() if isinstance(v, list)}
    if not (type(d.get("verdict")) is int and d["verdict"] in (0, 1, 2)):
        d["verdict"] = None
    space = d.get("space")                      # numbers and switches, and the tool that keeps the snapshots
    d["space"] = {k: v for k, v in space.items() if type(v) in (int, bool) or (k == "tool" and type(v) is str)} \
        if isinstance(space, dict) else None
    if not d["space"]:
        d.pop("space")
    if isinstance(d.get("unfinished"), list):   # package names only (they go into commands for the user)
        d["unfinished"] = [n for n in dict.fromkeys(plain(d["unfinished"])) if PKG_NAME.fullmatch(n)]
    else:
        d.pop("unfinished", None)
    return d


# The updater's own lines in a journal (RunLog.event): "[17:45:45 ПАКЕТЫ] обновлены: …". Looked for from the newline
# before them: a pattern with a fixed start is found quickly, and a journal can be megabytes long.
JOURNAL_EVENT = re.compile(r"\n\[(\d\d):(\d\d):(\d\d) ([^\]\n]{1,40})\] ?([^\n]*)")
PACMAN_PART = "\n══════ /var/log/pacman.log за это обновление ══════"     # after the updater's last lines
JOURNAL_WHOLE, JOURNAL_HEAD = 4 * 2 ** 20, 2 ** 19     # bytes of a journal read whole; of a longer one (a long AUR
                                                       # build) its head and the rest from its end, where the result is
JOURNAL_NAME = re.compile(r"20\d\d-\d\d-\d\d_\d\d-\d\d-\d\d\.log")


def journal_events(path, text=None):
    """The updater's own lines in a journal, up to its pacman.log part: [(hours, minutes, seconds, kind, text)].
    text: the journal as journal_text read it. Raises OSError."""
    text = journal_text(path) if text is None else text
    end = text.find(PACMAN_PART)
    return JOURNAL_EVENT.findall("\n" + (text if end == -1 else text[:end]))


def journal_text(path):
    """A journal, whole, or of a longer one its head and its end. Raises OSError."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size <= JOURNAL_WHOLE:
            data = f.read()
        else:
            data = f.read(JOURNAL_HEAD)
            f.seek(size - (JOURNAL_WHOLE - JOURNAL_HEAD))
            data += b"\n" + f.read()
    return data.decode("utf-8", "replace")


# mark_resolved's line, written after the pacman.log part like everything that comes after the update (a fix, R)
RESOLVED_LINE = re.compile(r"\n\[\d\d:\d\d:\d\d ИСПРАВЛЕНО\] ")


def journal_began(log):
    """When the update of a journal began (its name: local time), in seconds since the epoch; None for another name."""
    try:
        return time.mktime(time.strptime(os.path.basename(log)[:19], "%Y-%m-%d_%H-%M-%S"))
    except (ValueError, OverflowError):
        return None


def journal_result(log):
    """What a journal's own lines say of its update, for one without a summary (an older version's, or one whose
    summary a full disk did not let be written), shaped like a summary; None when it has no result at all (cut off,
    or still running)."""
    try:
        whole = journal_text(log)
    except OSError:
        return None
    result, started, problems, warnings = False, True, [], []
    for _, _, _, kind, text in journal_events(log, whole):
        text = text.rstrip()
        if kind == "ПАКЕТЫ" and text.startswith("обновлены:"):         # the checks' first line
            result = True
        elif kind == "НЕ НАЧАТО":                                      # only a boot partition not mounted is a problem
            result, started = True, False
            (problems if "не смонтирован" in text else warnings).append(text)
        elif kind == "ПРОБЛЕМА" and text != "arch-update завершился с кодом 11":     # a warning since
            problems.append(text)
        elif kind == "ВНИМАНИЕ":
            warnings.append(text)
    resolved = bool(RESOLVED_LINE.search(whole))
    if not result:
        return None
    began = journal_began(log)
    return normal_summary({"verdict": 2 if problems else 1 if warnings else 0, "problems": problems,
                           "warnings": warnings, "started": started, "resolved": resolved, "journal_only": True,
                           "began": datetime.datetime.fromtimestamp(began).isoformat() if began else None})


def unresolved(logdir=None, exclude=None):
    """The newest update of this very boot, if it found problems (verdict 2) and they are not fixed: until a reboot
    or a fix the menu keeps saying so. From its summary, or for one without (an older version's, or one a full disk
    did not let be written) from its journal's own lines. A run that did not start changed nothing: the update
    before it counts (unless that run is red itself, an unmounted /boot); one without a result (cut off, or still
    running) says nothing either way. exclude: a journal to pass over (the run being checked now). None otherwise."""
    save_pending()
    this_boot, boot_began = boot_id(), boot_started()
    skip = os.path.realpath(exclude) if exclude else None
    for log in sorted(glob.glob(os.path.join(logdir or logs_dir(), "20*.log")), reverse=True):
        if not JOURNAL_NAME.fullmatch(os.path.basename(log)) or (skip and os.path.realpath(log) == skip):
            continue
        data = read_json(log[:-4] + ".json")
        if data:
            if data.get("boot_id", "") != this_boot:
                return None
            data = normal_summary(data)
        else:
            began = journal_began(log)
            if began is None or boot_began is None or began < boot_began - 60:
                return None                         # an earlier boot's, or no telling
            data = journal_result(log)
            if data is None:
                continue
            if data["resolved"]:                    # a fix went through, and the problems were looked at again
                return None
        data["log"] = log
        if data.get("started", True) or data.get("verdict") == 2:
            # Marked fixed only once no problem was left (mark_resolved): one still there is still a problem.
            return data if data.get("verdict") == 2 else None
    return None


def unfinished_before(logdir=None, exclude=None):
    """The packages earlier updates left half written and the newest summary that keeps that list still names, from
    whatever boot (a reboot mends nothing there): {package: when the update that first named it began (ISO, "" when
    unknown)}, {} when there are none. A summary without the list (an update that did not start, another version's)
    is passed over. exclude: a journal to pass over (the run being checked now)."""
    skip = os.path.realpath(exclude) if exclude else None
    found, open_ = None, set()
    for data in summaries(logdir):
        if (skip and os.path.realpath(data["log"]) == skip) or "unfinished" not in data:
            continue
        began = data.get("began") if isinstance(data.get("began"), str) else ""
        if found is None:
            found = {n: began for n in data["unfinished"]}
            open_ = set(found)
        else:                                   # named there too: it began there (or earlier still)
            open_ &= set(data["unfinished"])
            found.update({n: began for n in open_})
        if not open_:
            break
    return found or {}


def journal_event(log, kind, msg):
    """An event line in the journal of that update, also after it has ended, and also when the update's own writes to
    it failed (a full disk: the run's log stops writing; once there is room again, this line gets there)."""
    save_pending()
    if not getattr(getattr(LOG, "txt", None), "closed", True) and os.path.realpath(LOG.path) == os.path.realpath(log) \
            and not LOG.broken:
        LOG.event(kind, msg)
        return
    try:
        with open(log, "ab+") as f:
            f.seek(0, os.SEEK_END)
            fresh = f.tell() == 0
            if not fresh:
                f.seek(-1, os.SEEK_END)
                fresh = f.read(1) == b"\n"
            f.write((("" if fresh else "\n") + f"[{datetime.datetime.now():%H:%M:%S} {kind}] {msg}\n").encode())
    except (OSError, TypeError, ValueError):
        pass


def _update_summary(log, change):
    """Read that update's summary, let change(data) alter it, recompute the verdict, write it back.
    Returns the summary, None if there is none or it cannot be written."""
    save_pending()
    path = summary_path(log)
    data = read_json(path)
    if not data:
        return None
    data = normal_summary(data)
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
    """The problems of that update are fixed (a fix went through and recheck_boot found nothing more): the menu
    stops warning about them. Only once its summary has no problem left: one that is not about booting (a pacman
    lock left behind, a failed hook) keeps it red, and the warning stays (False). For an update without a summary
    (a full disk did not let it be written) the caller has looked: its journal says it, which unresolved() reads.
    False also when nothing could be written."""
    save_pending()
    data = read_json(summary_path(log))
    if data:
        if data.get("problems"):
            return False
        done = _update_summary(log, lambda d: d.update(resolved=now_iso())) is not None
    else:
        done = isinstance(log, str) and os.path.isfile(log)
    if done:
        journal_event(log, "ИСПРАВЛЕНО", "проблемы этого обновления отмечены исправленными")
    return done


# ───────────────────────────── checks ─────────────────────────────

def _file_issue(kind, path, pkg, how, text):
    """A boot file missing, empty or broken (how): the kernel file, or an initramfs/UKI image."""
    return {"kind": "kernel" if kind == "kernel" else "image", "pkg": pkg, "path": path, "how": how, "text": text}


def initcpio_issue(marker):
    """The problem with booting a failure marker of mkinitcpio's says (INITCPIO_SAYS), None for another marker."""
    says = INITCPIO_SAYS.get(marker.lower()) if isinstance(marker, str) else None
    if not says:
        return None
    return {"kind": "initcpio", "text": f"{says}: «{marker}» — НЕ перезагружайся (сначала: sudo mkinitcpio -P)"}


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


def boot_state(probe=None, pacman_log=None, changed=(), earlier=(), since=None, found=(), event=_event, keep=False):
    """The boot partitions, the installed kernels' module lists and boot files, their DKMS modules and the Secure
    Boot signatures, as they are now. Returns (issues, warnings, ok, kernels): issues are the problems with booting
    as dicts {"kind", "text", …} (see fix_commands), kernels {package: version}.
    probe: what a probe found as root after the update (parse_probe); without one, or for what it did not look
    at, a probe is tried now (it works when sudo asks nothing). pacman_log: this update's part of pacman.log, to
    judge a /boot nobody could look into by what the update did to it. earlier: issues found before (by an
    earlier update of this boot, or by this one's own check); those this look can neither confirm nor rule out
    stay, as found then (since: when that was, None for this update's own). found: issues this update has
    found already (in its output), so that an earlier one of the same kind is not said twice. keep: a fix of
    them has just failed (True), or the fixes of these kernels (a set of their packages) were not done while the
    others' were: what this look does not find again stays all the same (a look that finds nothing wrong after a
    failed mkinitcpio may have missed what it left out), but for what this look has just seen exactly: a boot
    partition mounted, a DKMS module built, a kernel's module list there again, its kernel file there again and
    whole, and with a set, the images of every kernel not in it (their fixes were done and checked: what the look
    sees of them counts; mkinitcpio's own errors say no kernel, they stay). An empty set is False.
    A kernel file is whole when it is not smaller than the one its package installed (/usr/lib/modules/<version>/
    vmlinuz, which every user may read; the mkinitcpio hook copies it to /boot): a /boot that ran full leaves it cut
    short (GNU install keeps what it wrote), and the machine does not boot with it. sbctl signs a copy: larger."""
    issues, warnings, ok, unverified, looked_into = [], [], [], set(), set()
    whole_size = {}                             # kernel file -> the size of its package's own copy
    if isinstance(keep, (list, tuple)):
        keep = set(keep)
    for t in unmounted_boot():
        issues.append({"kind": "unmounted", "target": t,
                       "text": f"{t} не смонтирован — ядро могло записаться не туда, НЕ перезагружайся"})
    kernels = kernel_packages()
    # The module list (depmod writes it, the kernel package's hook runs depmod): without it nothing finds the
    # kernel's modules, mkinitcpio puts none into the initramfs and the machine does not boot.
    for pkg, kver in sorted(kernels.items()):
        if modular(kver) and not os.path.exists(module_index(kver)):
            issues.append({"kind": "depmod", "pkg": pkg, "kver": kver, "path": module_index(kver),
                           "text": f"у ядра {kver} нет списка модулей (depmod не отработал) — НЕ перезагружайся, "
                                   "система не загрузится"})
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
        elif kind == "kernel":
            try:
                whole_size[f] = os.path.getsize(os.path.join(MODULES_DIR, kver, "vmlinuz"))
            except OSError:
                continue                        # a kernel package that puts its file into /boot itself: as it is
            looked_into.add(f)
            if size < whole_size[f]:
                issues.append(_file_issue(kind, f, pkg, "short", f"{f} обрезан — НЕ перезагружайся, система с ним "
                                                                  f"не загрузится (сначала: sudo pacman -S {pkg})"))
        elif kind == "image":
            # Built for this kernel, and with its modules: mkinitcpio makes the kernel's folder in every image,
            # and its module list (modules.dep.bin) only when it put modules in (none: the image cannot boot).
            if f in readable:
                try:
                    r = subprocess.run(["lsinitcpio", f], capture_output=True, text=True, timeout=120)
                    for_kver = r.returncode == 0 and f"usr/lib/modules/{kver}/" in r.stdout
                    has_modules = bool(re.search(rf"(?m)^usr/lib/modules/{re.escape(kver)}/modules\.dep(?:\.bin)?$",
                                                 r.stdout))
                except (OSError, subprocess.SubprocessError):
                    for_kver = has_modules = False
            else:
                p = next((p for p in probes if f in p["images"]), None)
                if p is None:
                    unverified.add("images")    # a root-only image: its size is all anybody could see
                    continue
                rc, kvers = p["images"][f]
                indexed = p.get("indexed", {}).get(f)
                for_kver = rc == 0 and kver in kvers
                has_modules = indexed is None or kver in indexed     # None: a probe that did not say
            looked_into.add(f)
            if not for_kver:
                issues.append(_file_issue(kind, f, pkg, "broken", f"{f} обрезан или собран не под ядро {kver} — "
                                                                  "НЕ перезагружайся (сначала: sudo mkinitcpio -P)"))
            elif not has_modules and modular(kver):
                issues.append(_file_issue(kind, f, pkg, "broken", f"в {f} нет модулей ядра {kver} — НЕ "
                                                                  "перезагружайся, система с ним не загрузится "
                                                                  "(сначала: sudo mkinitcpio -P)"))
    rebuilt = bool(pacman_log) and bool(IMAGE_BUILT.search(pacman_log)) and not IMAGE_TROUBLE.search(pacman_log)
    if "files" in unverified:
        # No look inside: judge by what this update did to the boot files.
        if pacman_log is not None and not any(p in kernels for p in changed) and "mkinitcpio" not in pacman_log:
            ok.append("ядро и initramfs этим обновлением не менялись")
        elif rebuilt and not FAILURE_MARKERS.search(pacman_log):
            ok.append("initramfs собран заново (по выводу mkinitcpio)")
        else:
            warnings.append("не смог заглянуть в /boot: он закрыт для пользователя — "
                            "перед перезагрузкой проверь: sudo ls -l /boot")
    elif checked and not any(i["kind"] in ("unmounted", "kernel", "image", "depmod") for i in issues):
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
        if kind in ("unmounted", "dkms") or (kind == "depmod" and path):
            return True                         # looked at every time
        if kind == "depmod":                    # depmod failed then: its lists written again since, for every kernel
            at = i.get("at") if isinstance(i.get("at"), (int, float)) else time.time()
            return bool(kernels) and all(_mtime(module_index(k)) > at for k in kernels.values())
        if kind in ("kernel", "image"):
            return path in looked_into if i.get("how") in ("broken", "short") else path in known
        if kind == "initcpio":                  # mkinitcpio failed then: a new image built since, or all looked into
            return rebuilt or not ({"files", "images"} & unverified)
        if kind == "unsigned":
            return "sb" not in unverified
        return False                            # a kernel partition to flash by hand: nothing here can tell

    def settled(i):
        """What this look sees exactly, and has just looked at: it counts even after a fix that failed. Not an
        image (unless its kernel's fixes were all done: keep a set without its package), mkinitcpio's errors or a
        signature: a look that finds nothing wrong there may miss what is."""
        kind, path = i.get("kind"), i.get("path")
        if kind in ("unmounted", "dkms") or (kind == "depmod" and path):
            return True
        if kind == "image":
            return isinstance(keep, (set, frozenset)) and isinstance(i.get("pkg"), str) and i["pkg"] not in keep
        size = known.get(path)
        return kind == "kernel" and i.get("how") in ("missing", "empty", "short") and bool(size) \
            and size >= whole_size.get(path, 0)
    now = {(i["kind"], i.get("path")) for i in list(issues) + list(found)}
    for i in earlier:
        if not isinstance(i, dict) or not isinstance(i.get("text"), str) \
                or (seen_again(i) and (not keep or settled(i))):
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
    if any(i.get("kind") in ("kernel", "image", "initcpio", "depmod") for i in issues):
        ok = [o for o in ok if not re.match(r"(?:ядро|ядра) и initramfs", o)]   # not with a problem still there
    return issues, warnings, ok, kernels


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


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


def not_started(why, verdict, boot=None, space=None):
    """Result of a run that never touched the system (refused before it began). boot: the problems with
    booting that stopped it (an unmounted /boot), for fix_commands; space: the disk was too small for it
    (Responder.space_short), for the space fix."""
    LOG.event("НЕ НАЧАТО", why)
    notify("Обновление не начато", why, urgent=verdict == 2)
    res = {"changed": [], "added": [], "removed": [], "problems": [why] if verdict == 2 else [],
           "warnings": [why] if verdict < 2 else [], "notes": [], "ok": [], "verdict": verdict, "log": LOG.path,
           "exitstatus": None, "started": False, "head": "Обновление не начиналось", "services": services_record(),
           "boot": list(boot or []), "reboot": False}
    if space:
        res["space"] = space
    res["fixes"] = fix_commands(res)
    save_summary(res)
    return res


def space_freed(log):
    """After the space fix went through: that update's summary no longer says the disk was too small (an update
    that stopped for it can run now). A full disk during an update stays a problem: that update is to run again."""
    def change(d):
        d["warnings"] = [w for w in d.get("warnings") or [] if not w.startswith(SPACE_HEAD) and w != FULL_BEFORE]
        d["problems"] = [p for p in d.get("problems") or [] if not p.startswith(SPACE_HEAD) and p != FULL_BEFORE]
        d.pop("space", None)
        d["ok"] = list(d.get("ok") or []) + [f"место освобождено: свободно {size_text(shutil.disk_usage('/').free)}"]
    try:
        data = _update_summary(log, change) if log else None
    except OSError:
        data = None
    journal_event(log, "МЕСТО", "освобождено по клавише F")
    return data


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
    depmod_failed, refused_space = False, None
    for f in sorted(responder.failures):
        flash = KERNEL_FLASH.search(f)
        initcpio = initcpio_issue(f)
        if flash:
            boot.append({"kind": "flash", "cmd": flash.group(0),
                         "text": "новое ядро не записано в раздел ядра — перед перезагрузкой выполни: "
                                 f"sudo {flash.group(0)}"})
        elif "update process" in f:
            warnings.append(f"в выводе: «{f}»")
        elif initcpio:
            boot.append(initcpio)
        elif f.lower() == "depmod: fatal":
            depmod_failed = True                # for which kernels: the look below knows them
        elif f.lower() == "no space left on device":
            problems.append(DISK_FULL)
        elif f.lower() == "not enough free disk space":
            # A later step's pacman refused (the AUR step's): its own count, for the space fix, when / was too full
            _, refused_space = pacman_refusal(responder) or ("", None)
            problems.append("pacman не стал ставить часть пакетов: "
                            + (space_facts(refused_space) if refused_space else
                               "мало места на диске («not enough free disk space»)")
                            + " — освободи место и обнови ещё раз")
        elif f not in UNWRITTEN.values():       # files pacman could not write: below
            problems.append(f"в выводе: «{f}»")
    # Files pacman could not write. "could not extract" is a full file system: / when it has (next to) no room left
    # now, or the output said so; else another one (a full /boot: pacman puts the microcode there). "warning given"
    # is a write that failed otherwise (an I/O error, a quota): the file was cut short, pacman went on. Either way
    # the packages stay registered as installed: updating again does not mend them, a reinstall does (one line for
    # each way of doing it, see unfinished_lines). The packages pacman could not finish are named whatever pacman
    # said of their files (a full disk can fail it at a file it could not create, "Can't create", or at its
    # database entry, which then has to be taken out first).
    unwritten = getattr(responder, "unwritten", None) or {}
    unfinished = [n for n in getattr(responder, "unfinished", None) or [] if isinstance(n, str)]
    no_entry = [n for n in getattr(responder, "unwritten_entries", None) or [] if isinstance(n, str)]
    unfinished += [n for n in no_entry if n not in unfinished]
    cut = []                                    # packages a write error cut short, pacman going on
    left = free_mib("/")
    full = left is not None and left < FULL_LEFT
    for marker in (m for m in UNWRITTEN.values() if m in responder.failures):
        files = [p for p in unwritten.get(marker) or [] if isinstance(p, str)]
        example = f" (например, {files[0]})" if files else ""
        if marker == UNWRITTEN["could not extract"] and (full or DISK_FULL in problems):
            if DISK_FULL not in problems:       # (in it already: "No space left on device" in the output)
                problems.append(DISK_FULL)
        elif marker == UNWRITTEN["could not extract"]:
            problems.append(f"pacman не смог записать часть файлов{example}: там кончилось место — освободи его "
                            "(df -h покажет, где его мало)" + ("" if unfinished else ", потом обнови ещё раз"))
        else:
            cut = [n for n in owners(files) if n not in unfinished]
            problems.append(f"pacman записал часть файлов не целиком{example}: ошибка записи на диск — проверь диск"
                            + ("" if cut or unfinished else ", потом поставь эти пакеты заново"))
    if exitstatus not in (0, None, 11) and full and DISK_FULL not in problems:
        problems.append(DISK_FULL)              # it failed, and / is full: whatever it said, room is the first thing
    room = DISK_FULL in problems or UNWRITTEN["could not extract"] in responder.failures
    problems += unfinished_lines(unfinished, True, no_entry, full=room, installed=after)
    problems += unfinished_lines(cut, False, installed=after)
    # Those an earlier update left half written (from whatever boot): looked at again, as pacman sees them now.
    still, whole_again = {}, []
    earlier_unfinished = unfinished_before(LOG.dir, exclude=LOG.path) if carry else {}
    if earlier_unfinished:
        as_root = elevation_command() == "sudo" and not sudo_asks_password()
        for name, began in earlier_unfinished.items():
            if name in unfinished or name in cut:
                continue                        # named again just now
            state = package_state(name, as_root)
            if state is None:
                LOG.event("ПАКЕТЫ", f"{name} больше не установлен — проверять нечего")
            elif state["whole"]:
                whole_again.append(name)
            else:
                still[name] = (_when(began), state["entry"])
    for since in dict.fromkeys(s for s, _ in still.values()):
        names = [n for n, (s, _) in still.items() if s == since]
        problems += unfinished_lines(names, True, [n for n in names if not still[n][1]], since=since,
                                     installed=after)
    if whole_again:
        ok.append(("пакет " if len(whole_again) == 1 else "пакеты ") + ", ".join(whole_again)
                  + (" снова цел" if len(whole_again) == 1 else " снова целы"))
    if any(KEYRING_TROUBLE.search(f) for f in responder.failures):
        keyring = "archlinuxarm-keyring" if before.get("archlinuxarm-keyring") else "archlinux-keyring"
        problems.append(f"похоже, устарел keyring: sudo pacman -Sy {keyring} && sudo pacman -Su, потом обнови снова")

    # What an earlier update of this boot found wrong with booting and nobody fixed (its summary, or for one
    # without, its journal); this run's own journal says nothing yet.
    prev = unresolved(LOG.dir, exclude=LOG.path) if carry else None
    issues, w, o, kernels = boot_state(probe, pacman_log, changed + added, earlier=(prev or {}).get("boot") or (),
                                       since=prev and _when(prev.get("began")) or None, found=boot)
    boot += issues
    if depmod_failed and not any(i["kind"] == "depmod" for i in issues):
        # The lists are there (from before?): depmod failed on them all the same, a module may be missing in them.
        kvers = sorted(kernels.values())
        boot.append({"kind": "depmod", "kvers": kvers, "at": time.time(),
                     "text": "depmod не обновил список модулей ядра («depmod: FATAL») — НЕ перезагружайся"
                             + (f" (сначала: sudo depmod {kvers[0]})" if len(kvers) == 1 else "")})
    problems += [i["text"] for i in boot]
    warnings += w
    ok += o

    kernel_new = bool(kernels) and running_kernel() not in kernels.values()
    reboot_pkgs = [p for p in changed + added if REBOOT_PKGS.match(p) or p in kernels]
    reboot = kernel_new or bool(reboot_pkgs) or ("reboot" in seen_notes and not kernels)
    if problems and reboot:
        # Never "reboot when you like" next to a problem (one an earlier update of this boot left included).
        notes.append("перезагрузиться понадобится" + (" (обновилось ядро)" if kernel_new else
                                                       f" (обновились: {', '.join(reboot_pkgs[:8])})" if reboot_pkgs
                                                       else "") + " — но только когда проблемы выше будут исправлены")
    elif kernel_new:
        notes.append("обновилось ядро — перезагрузись, когда будет удобно"
                     + (f" (обновились: {', '.join(reboot_pkgs[:8])})" if reboot_pkgs else ""))
    elif reboot_pkgs:
        notes.append("перезагрузись, когда будет удобно (обновились: " + ", ".join(reboot_pkgs[:8]) + ")")
    elif "reboot" in seen_notes and not kernels:      # no kernel of ours to judge by: trust arch-update
        notes.append("arch-update советует перезагрузиться — когда будет удобно")
    elif "reboot" in seen_notes:
        LOG.event("ЯДРО", "arch-update предлагал перезагрузку, но работающее ядро на месте — пропускаю")
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

    space = getattr(responder, "space", None)
    if space:
        warnings.append(f"{SPACE_HEAD} — часть обновления не ставил: {space_facts(space)}; освободи место (F) и "
                        "обнови ещё раз" if space.get("stopped") else
                        f"{SPACE_HEAD} перед установкой — освободи место (F), пока его не стало ещё меньше: "
                        f"{space_facts(space)}")
    if LOG.broken:
        warnings.append("журнал обновления неполный: не удалось писать на диск (место в /home?)")
    for p in problems:
        LOG.event("ПРОБЛЕМА", p)
    for w in warnings:
        LOG.event("ВНИМАНИЕ", w)
    for n in notes:
        LOG.event("ЗАМЕТКА", n)
    if pacman_log is not None:
        LOG.text(PACMAN_PART + "\n" + pacman_log)

    if problems:
        notify("Обновление: нужна проверка", "\n".join(problems), urgent=True)
    else:
        clean = "Всё прошло чисто. Перезагрузись, когда будет удобно." if reboot else "Всё прошло чисто."
        notify("Обновление завершено", "\n".join(warnings) or clean)
    res = {"changed": changed, "added": added, "removed": removed, "problems": problems,
           "warnings": warnings, "notes": notes, "ok": ok, "verdict": 2 if problems else (1 if warnings else 0),
           "log": LOG.path, "exitstatus": exitstatus, "started": True, "services": services, "boot": boot,
           "reboot": reboot,
           "unfinished": [n for n in dict.fromkeys(unfinished + cut + list(still)) if PKG_NAME.fullmatch(n)]}
    if getattr(responder, "space", None):
        res["space"] = responder.space          # too little room for a part of it (an AUR package, or a manual run)
    elif refused_space:
        res["space"] = dict(refused_space, stopped=True)    # pacman refused a part of it: what it said it needs
    res["fixes"] = fix_commands(res)
    save_summary(res, responder)
    return res


# ───────────────────────────── fixes ─────────────────────────────

def fix_commands(res):
    """What can be done about the problems a result (or a summary read back) holds, in the order to run them: the
    fixes catalogue (lib/fixes, one module per fix) planned for it, [{"key", "id", "label", "argv", "how", "args"}]
    (id: the key, or "<key>@<kernel package>" for a fix of one kernel: one kernel's fix that is not done holds up
    only that kernel's later fixes):
      space      the disk too full for the update, or full during it
      mount      the fstab boot partitions that are not mounted
      depmod     a kernel's module list not written, or depmod failed (a full disk); ldconfig with it
      kernel     a kernel file missing or empty: that very version of the kernel package again (per kernel)
      dkms       a DKMS module not built for a kernel (per kernel)
      initramfs  an initramfs (or UKI) missing, empty or broken, mkinitcpio failed, or after depmod / DKMS (per
                 kernel, only the kernels concerned)
      sbctl      the boot files not signed for Secure Boot
    Each one backs up what it changes, checks what it made and puts everything back when that is not right
    (see lib/fixes). [] when there is nothing to do this way. They ask for the password: run them in a terminal
    (fixes_command)."""
    return catalogue.plan(res)


def cached_package(pkg, version):
    """The file of that package version in pacman's cache, None when it is not there."""
    want = re.compile(re.escape(f"{pkg}-{version}-") + PKG_ARCH + r"\.pkg\.tar(?:\.\w+)?")
    try:
        found = sorted(n for n in os.listdir(PACMAN_CACHE) if want.fullmatch(n))
    except OSError:
        return None
    return os.path.join(PACMAN_CACHE, found[-1]) if found else None


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


def boot_probe_command(argv, out, ask_first=False):
    """argv (a fix) followed, in the same terminal session, by a probe of the boot files into the file out:
    after `sudo …` asked for the password in that terminal, only that session can look into a closed /boot.
    Give out to recheck_boot afterwards. The exit status is argv's. ask_first: the password at the start, and
    kept fresh while argv runs (ASK_SUDO_FIRST: a fix can take longer than sudo remembers it)."""
    files, images = probe_targets()
    return probe_command(argv, out, files, images, secure_boot_on() and bool(shutil.which("sbctl")),
                         ask_first=ask_first)


def fixes_command(fixes, out, result=None):
    """All the fixes as one command for one key: the catalogue's root shell (sudo asks once), every fix in order with
    its backups, check and rollback, stopping at the first that was not done; then, in the same terminal session,
    the probe into out for recheck_boot(log, probe=out). result: a file of this user's (fixes.result_file()) for
    what came of each fix (fixes.read_results). Run it in a pty: sudo asks for the password there, pacman and the
    space fix ask before they change anything. Exit status: the runner's (fixes.OK … fixes.NOT_ENOUGH)."""
    # sudo that asks for a password: asked once, at the start, and kept fresh (the space fix alone may wait
    # minutes for an answer and for btrfs), so the look right after the fixes still has the ticket
    ask = elevation_command() == "sudo" and sudo_asks_password()
    return boot_probe_command(catalogue.command(fixes, result) or ["true"], out, ask_first=ask)


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


def recheck_boot(log, probe=None, failed=False, markers=()):
    """The problems with booting of that update as they are now (after a fix): the same look as after the update
    (boot partitions, the kernels' module lists and boot files, DKMS, Secure Boot). What cannot be looked at now
    (a /boot closed to the user while sudo asks for a password) stays as it was found; after a fix that failed
    (failed: True when its exit status was not 0, or its output said an error) so does everything found before
    that this look does not find again, but what it has just seen exactly (boot_state's keep). failed may also be
    the set of kernel packages whose fixes were not done, when every fix for the whole machine was: then the images
    of the other kernels count as the look sees them (their fixes were done and checked), those of these kernels and
    mkinitcpio's errors stay as found (an empty set: nothing failed). The update's summary follows: its problems,
    verdict and fixes. probe: the file boot_probe_command wrote (or parse_probe's dict).
    markers: what FAILURE_MARKERS found in the output of the fixes whose changes stayed (a kernel reinstalled, and
    its mkinitcpio hook said an error): mkinitcpio's (INITCPIO_SAYS) are problems with booting, as after the
    update, each once. Returns the problems with booting, [] when there are none."""
    save_pending()
    data = normal_summary(read_json(summary_path(log)))
    old = [i for i in data["boot"] if isinstance(i.get("text"), str)]
    if isinstance(probe, str):
        probe = read_probe(probe)
    said = {}
    for m in sorted(x for x in markers or () if isinstance(x, str)):
        issue = initcpio_issue(m)
        if issue:
            said.setdefault(m.lower(), issue)
    said = list(said.values())
    issues, _, _, _ = boot_state(probe if isinstance(probe, dict) else None, earlier=old, keep=failed,
                                 found=said, event=lambda kind, msg: journal_event(log, kind, msg))
    issues = said + issues
    texts = [i["text"] for i in issues]
    gone = {i["text"] for i in old}

    def change(d):
        d["problems"] = [p for p in d.get("problems") or [] if p not in gone] + texts
        d.update(boot=issues, fixes=fix_commands({"boot": issues}), rechecked=now_iso())
    if read_json(summary_path(log)):
        _update_summary(log, change)
    journal_event(log, "ПЕРЕПРОВЕРКА", ("после неудачного исправления: " if failed else "")
                  + ("; ".join(texts) or "проблем с загрузкой не нашёл"))
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
    save_pending()                              # a summary the last one's full disk kept back: before this one
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
    responder = Responder(child, stop_on_news, manual, restart_services=restart, check_space=not demo)
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
    full = "not enough free disk space" in {f.lower() for f in responder.failures}
    if (responder.space or full) and installed_versions() == before:
        # stopped at pacman's question for want of room (or pacman found too little itself): nothing changed
        if responder.space:
            why, space = space_text(responder.space), responder.space
        else:                                   # pacman's own count when it said it, else the space fix's own
            why, space = pacman_refusal(responder) or \
                (FULL_BEFORE, {"need": catalogue.space.AFTER_FULL, "free": shutil.disk_usage("/").free})
        return status, not_started(why, 1, space=space), responder
    result = system_checks(before, status, responder, pacman_log_offset, failed_before, orphans_before=orphans_before,
                           probe=probe, carry=not demo) if summary else None
    return status, result, responder


import fixes as catalogue  # noqa: E402  (the fixes catalogue, lib/fixes: it uses the engine above)
