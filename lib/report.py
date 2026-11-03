#!/usr/bin/env python3
"""Update reports, only with the user's consent: after each update a report of it goes to the
developer, so that a problem on any machine can be looked into without asking for the log.

A report holds the update's result (verdict, changed packages, the post-update checks), what the
machine is (model, CPU, GPU, memory, kernel, desktop, terminal, and how it is connected: Wi-Fi or
cable, the adapter, the signal) and the update's log. Taken out of all of it: the user's and the
host's names, home paths, IP and MAC addresses, e-mail addresses, the machine ID, passwords and
tokens in web addresses, the instance names of systemd units (a user or a Wi-Fi network can be
named there) and a password typed before its prompt showed. The name of the Wi-Fi network is never
read. While reports are off (the default) nothing is collected and nothing goes out but the one
report the user sends by hand: a key on the summary or in the list of the last updates, or
report.py --send N. That key is the consent for that one report and switches nothing on. The demo
never reports.

Everything stays in ~/.local/state/arch-update-auto: settings.json (the choices, a random report
ID), reports/outbox (waiting to be sent), reports/sent (the last ones that went out) and
reports/manual.json (which updates' reports went out, with their issues). The updates themselves
are the journals in logs/ (with a summary, <stamp>.json, from the engine of this version on).

  report.py --consent yes|no   record the user's choice (the installer's question)
  report.py --decided          exit 0 if the user has chosen either way
  report.py --status           one line for the installer: on or off
  report.py --send             send what waits in the outbox (started detached after an update)
  report.py --preview          what a report from this machine looks like now
  report.py --list             the last updates kept on this machine, numbered: 1 is the newest
  report.py --send N           send the report of update N of that list now, with reports off too
  report.py --send-log PATH    the same for the update of that journal (one in logs/)
"""
import contextlib
import datetime
import fcntl
import glob
import http.client
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import engine  # noqa: E402

# Where reports go: the relay (relay/worker.js, on Cloudflare) that files them in the developer's private
# repository on GitHub. ARCH_UPDATE_REPORT_URL overrides it ("" means no server: reports only wait).
ENDPOINT = "https://arch-update-reports.cirill11100.workers.dev/report"
SCHEMA = 1
SETTINGS = os.path.join(engine.STATE_DIR, "settings.json")
REPORTS = os.path.join(engine.STATE_DIR, "reports")
OUTBOX = os.path.join(REPORTS, "outbox")
SENT = os.path.join(REPORTS, "sent")
REJECTED = os.path.join(REPORTS, "rejected")
STATUS = os.path.join(REPORTS, "status.json")
KEEP = {OUTBOX: 20, SENT: 20, REJECTED: 5}
TRIES = 3                           # times the relay may fail on one report (500, 502) before it is set aside
LOG_LIMIT = 200_000                 # characters of log in one report: the head and the tail of a longer one
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# The updates kept on this machine: the journals of engine.RunLog (<stamp>.log, .raw and the summary <stamp>.json),
# never the demo's (demo-logs/). Which of them went out, and as which issue, manual.json says.
LOGS = os.path.join(engine.STATE_DIR, "logs")
RECORDS = os.path.join(REPORTS, "manual.json")
SHOWN = 5                           # updates the list of the last ones offers
PER_HOUR = 6                        # reports the relay takes from one machine in an hour (relay/worker.js PER_ID)


def endpoint():
    return os.environ.get("ARCH_UPDATE_REPORT_URL", ENDPOINT)


# ───────────────────────────── the choice ─────────────────────────────

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)                    # a full disk leaves no half-written file behind
        raise


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _str(value):
    return value if isinstance(value, str) else ""


def settings():
    return _read_json(SETTINGS)


def enabled():
    return settings().get("reports") is True


def decided():
    return "reports" in settings()


def set_consent(on):
    """The user's choice. Switching off drops what was waiting to be sent, before anything else: if
    the choice cannot be written then, settings.json goes (without it reports are off) and the error
    is raised all the same."""
    with _locked():                     # an ID a report sent by hand makes meanwhile is kept (ensure_id)
        s = settings()
        s["reports"] = bool(on)
        s["reports_decided"] = datetime.date.today().isoformat()
        if on and not _id_ok(s.get("report_id")):
            s["report_id"] = secrets.token_hex(4)      # random: not derived from anything on the machine
        if not on:
            for f in glob.glob(os.path.join(OUTBOX, "*.json")):
                _remove(f)
        try:
            _write_json(SETTINGS, s)
        except OSError:
            if not on:
                _remove(SETTINGS)
            raise
    return s


def _id_ok(rid):
    return isinstance(rid, str) and re.fullmatch(r"[0-9a-f]{8}", rid) is not None


def report_id():
    rid = settings().get("report_id")
    return rid if _id_ok(rid) else ""


_UNSAVED = None                     # an ID made for a report sent by hand, until one went out with it


def ensure_id(save=True):
    """This machine's report ID, made now if it has none: for a report sent by hand, without switching the automatic
    reports on or counting as the choice about them. save=False: only for the report about to go (and for the next
    one this program sends); it is saved with save=True once one went out, so that a machine nothing ever went out
    from has none in its settings. One that cannot be saved still serves this report."""
    global _UNSAVED
    rid = report_id()
    if rid:
        return rid
    _UNSAVED = _UNSAVED or secrets.token_hex(4)       # random: not derived from anything on the machine
    if not save:
        return _UNSAVED
    try:
        with _locked():
            s = settings()
            if _id_ok(s.get("report_id")):            # made meanwhile
                return s["report_id"]
            s["report_id"] = _UNSAVED
            _write_json(SETTINGS, s)
    except OSError:
        pass
    return _UNSAVED


def option(key, default=None):
    """One setting of settings.json (restart_services …): default when it is not there, or is of another type than
    default (a switch written by hand as "yes")."""
    value = settings().get(key)
    if value is None or (default is not None and type(value) is not type(default)):
        return default
    return value


def set_option(key, value):
    """One setting changed and the others kept, written whole or not at all; raises OSError when it cannot be
    written. The reports' own switch goes through set_consent: off drops what waits to be sent."""
    if key == "reports":
        return set_consent(value)
    with _locked():
        s = settings()
        s[key] = value
        _write_json(SETTINGS, s)
    return s


@contextlib.contextmanager
def _locked():
    """Held while settings.json or manual.json is read, changed and written back: the program, the sender in its
    own process and the command line may be at it at once. Waits three seconds at most, and goes on without
    when no lock can be had."""
    lock = None
    try:
        os.makedirs(REPORTS, exist_ok=True)
        lock = open(os.path.join(REPORTS, "records.lock"), "a")
        for _ in range(60):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.05)
    except OSError:
        pass
    try:
        yield
    finally:
        if lock:
            lock.close()


# ───────────────────────────── taking out what is personal ─────────────────────────────

# Words that are names often enough on Linux machines ("arch", "user") but are everywhere in an update
# log: replacing every one of them would ruin the log, and as a user or host name they say nothing.
COMMON = {"arch", "archlinux", "linux", "user", "admin", "root", "home", "test", "pc", "computer", "laptop",
          "desktop", "chromebook", "localhost", "penguin", "manjaro", "endeavouros", "cachyos", "garuda"}
# The words of what the scrubber puts in: a name that is one of them (Mac) is not looked for inside <mac>.
PLACEHOLDERS = {"user", "host", "name", "email", "mac", "secret", "credentials", "instance"}
MAC = re.compile(r"(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])")
IPV4 = re.compile(r"(?<![\w.-])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
                  r"(?![\w.-])")
IPV6 = re.compile(r"(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}"
                  r"|(?:[0-9A-Fa-f]{1,4}:)+:(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4})(?![\w:.])")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){0,8}\.(?!(?:service|socket|timer|mount|"
                   r"target|path|slice|scope|device|swap|automount)\b)[A-Za-z]{2,24}\b")
# What a web address carries before "@" (user:password, or a token alone): makepkg prints the source
# it failed to fetch as it is. Found from "://", never from a scheme: a pattern that starts with the
# scheme's letters tries every word boundary of a long run again (quadratic on "a.a.a.a…").
CREDENTIALS = re.compile(r"://[^\s/?#@:'\"<>]+(?::[^\s/?#'\"]*)?@")
# A secret in a query or a fragment: ?token=…, &api_key=…, #access_token=…, X-Amz-Signature=…
SECRET_PARAM = re.compile(r"(?i)([?&;#][\w.~%+-]{0,40}?(?:token|auth|key|pass|pwd|secret|sig|credential|jwt|session"
                          r"|x-amz-|x-goog-)[\w.~%+-]{0,40}=)[^\s&#;'\"<>()\[\],]+")
# A password typed before its prompt showed is echoed on a line of its own, right above the prompt.
TYPED_AHEAD = re.compile(r"(?im)^\S{1,128}\n(?=[^\n]*?(?:password|passphrase|пароль))(?=[^\n]*:[^\S\n]*$)")
TYPED_AHEAD_SAYS = "<ввод до запроса пароля>\n"
# In a manual update the keys go straight to the program, and whatever is typed before a password prompt
# is echoed into the journal: with spaces, typed twice, or ahead on the prompt's own line. Such a journal
# loses, around each password prompt, the start of the prompt's line and up to three lines above it that
# are not the updater's own notes ("[12:34:56 СТАРТ] …").
PASSWORD_PROMPT = re.compile(r"(?i)(?:\[sudo\] )?(?:password|passphrase|пароль)[^\n]{0,120}?:[^\S\n]*$")
OWN_NOTE = re.compile(r"\[\d\d:\d\d:\d\d \S")
SWITCHED_TO_MANUAL = re.compile(r"(?m)^\[\d\d:\d\d:\d\d РУЧНОЙ\]")       # Ctrl+] in an automatic update
# A templated unit's instance can be a user (syncthing@cactus.service) or a Wi-Fi network (netctl names
# its profiles "interface-network": netctl@wlp2s0\x2dHome.service); a number (user@1000.service) or a
# tty (getty@tty1.service) says nothing, if that is all of it (ping@192.168.1.5.service is no number).
# Unit names are at most 255 characters.
UNIT_TYPE = r"\.(?:service|socket|timer|path|target)\b"
UNIT_INSTANCE = re.compile(r"@(?!(?:\d+|tty\w*)" + UNIT_TYPE + r")[\w:.\\-]{1,255}(" + UNIT_TYPE + ")")
# Right after a user name in a unit's name: more of a path (home-cactus-Music.mount, systemd writes a "-"
# in a folder's name as \x2d) or the unit's end (home-cactus.mount).
UNIT_AFTER_NAME = (r"(?=(?:-|\\x2d)[\w.\\-]{0,255}\.(?:mount|automount|swap)\b"
                   r"|\.(?:mount|automount|swap|service|socket|timer|path|target)\b)")
HOME_MOUNT = re.compile(r"(?<![\w-])((?:run-media|home)-)(?!<user>)[^\s,.@\\-]{1,64}(?:\\x2d[^\s,.@\\-]{1,64}){0,8}"
                        r"(?=(?:-|\\x2d)[\w.\\-]{0,255}\.(?:mount|automount)\b|\.(?:mount|automount)\b)")
# Where a journal shows an account's or a machine's name whatever it is (names_in)
OLD_USER = re.compile(r"(?i)(?:password for|пароль для) ([^\s:()@<>]{1,64}):")
OLD_DOAS = re.compile(r"(?i)\(([^\s:()@<>]{1,64})@([^\s:()@<>]{1,253})\) password")
OLD_HOME = re.compile(r"(?<![\w.-])/home/([^/\s'\"<>:;,()\[\]{}]{1,64})(?=/|[\s'\":;,)\]}]|$)")


def _who():
    """(user names, host names, home folders, machine ID) of this machine, to be taken out."""
    users, names, homes = set(), set(), set()
    try:
        pw = pwd.getpwuid(os.getuid())
        users.add(pw.pw_name)
        homes.add(pw.pw_dir)
        # the real name, if one is set (not the room or the phone numbers that can follow it)
        names.update(p for p in re.split(r"[\s,]+", pw.pw_gecos) if re.search(r"[^\W\d_]", p))
    except KeyError:
        pass
    users.update(filter(None, (os.environ.get("USER"), os.environ.get("LOGNAME"))))
    homes.add(os.path.expanduser("~"))
    hosts = {socket.gethostname()}
    try:
        with open("/etc/hostname") as f:
            hosts.add(f.read().strip())
    except OSError:
        pass
    hosts.update({h.split(".")[0] for h in hosts})
    try:
        with open("/etc/machine-id") as f:
            machine = f.read().strip()
    except OSError:
        machine = ""
    homes = {h.rstrip("/") for h in homes if h}
    return users - {""}, hosts - {""}, names - {""}, homes - {""}, machine


def names_in(text):
    """(user names, host names, home folders' names) a journal itself shows where only such a name can stand: in a
    password prompt ("[sudo] password for …:", doas's "(…@…) password:"), as a home folder (/home/…/: a folder
    such as /home/build is no name to take out of every other word). A journal kept for weeks can be from before
    the account or the machine was renamed, and it is sent by hand that late too."""
    users, hosts = set(OLD_USER.findall(text if isinstance(text, str) else "")), set()
    for user, host in OLD_DOAS.findall(text if isinstance(text, str) else ""):
        users.add(user)
        hosts.update({host, host.split(".")[0]})
    homes = set(OLD_HOME.findall(text if isinstance(text, str) else ""))

    def real(found):
        return {n for n in found if n and "<" not in n}
    return real(users), real(hosts), real(homes) - real(users)


def scrubber(extra_users=(), extra_hosts=(), extra_homes=()):
    """A function that takes everything personal out of a text (see the module's doc). What it leaves
    it leaves again: a text scrubbed twice is the text scrubbed once. extra_users, extra_hosts: more names to take
    out besides this machine's own; extra_homes: names to take out only where they are a home folder or a user
    (names_in)."""
    users, hosts, names, homes, machine = _who()
    users, hosts = users | set(extra_users), hosts | set(extra_hosts)
    rules = [(TYPED_AHEAD, TYPED_AHEAD_SAYS)]
    for h in sorted(homes, key=len, reverse=True):            # /home/cactus/... → ~/...
        rules.append((re.compile(r"(?<![\w.-])" + re.escape(h) + r"(?![\w.-])"), "~"))
    for u in users | set(extra_homes):
        for h in hosts:
            rules.append((re.compile(re.escape(u) + "@" + re.escape(h), re.I), "<user>@<host>"))
    # In these places a user name is one whatever it is, a common word too (see COMMON).
    for u in sorted(users | set(extra_homes), key=len, reverse=True):
        rules.append((re.compile(r"(?i)(password for |пароль для |/home/|user |пользовател[ья] )" + re.escape(u)
                                 + r"(?![\w-])"), r"\1<user>"))
    # Before the e-mail rule (bob:secret@example.com is no address) and that before the names
    # (cactus@cactus.dev is an address, not a user at some host).
    rules += [(CREDENTIALS, "://<credentials>@"), (SECRET_PARAM, r"\1<secret>"), (EMAIL, "<email>")]
    # Names inside unit names: home-cactus.mount, run-media-cactus-USB.mount, mnt-Ivan.mount; then the account
    # in a home or removable-media mount whoever it is (another account on the machine too).
    for words, repl, everyone in ((users, "<user>", True), (hosts, "<host>", False), (names, "<name>", False)):
        for w in sorted(words, key=len, reverse=True):
            if not everyone and (len(w) < 3 or w.lower() in COMMON):
                continue
            forms = "|".join(re.escape(f) for f in sorted({w, w.replace("-", r"\x2d")}, key=len, reverse=True))
            rules.append((re.compile(r"(?i)(?:(?<=[-@])|(?<=\\x2d))(?:" + forms + ")" + UNIT_AFTER_NAME), repl))
    rules.append((HOME_MOUNT, r"\1<user>"))
    # A name ends where a word does: before a "." too (zork-laptop.local, "set to zork-laptop."), but
    # not before a "-" (arch-update-cactus is no user's name).
    plain, guarded = (r"(?<![\w-])", r"(?![\w-])"), (r"(?<![\w<-])", r"(?![\w>-])")
    for words, repl in ((users, "<user>"), (hosts, "<host>"), (names, "<name>")):
        for w in sorted(words, key=len, reverse=True):
            if len(w) >= 3 and w.lower() not in COMMON:
                before, after = guarded if w.lower() in PLACEHOLDERS else plain
                rules.append((re.compile(before + re.escape(w) + after, re.I), repl))
    rules.append((UNIT_INSTANCE, r"@<instance>\1"))           # after the names: syncthing@<user>.service stays
    if re.fullmatch(r"[0-9a-f]{32}", machine):
        rules.append((re.compile(machine, re.I), "<machine-id>"))
    rules += [(MAC, "<mac>"), (IPV4, "<ip>"), (IPV6, "<ip>")]

    def scrub(text):
        for rx, repl in rules:
            text = rx.sub(repl, text)
        return text
    return scrub


def hide_typed_ahead(log):
    """A manual update's journal without what was typed ahead of its password prompts (see PASSWORD_PROMPT)."""
    lines = log.split("\n")
    for i, line in enumerate(lines):
        m = PASSWORD_PROMPT.search(line)
        if not m or OWN_NOTE.match(line):
            continue
        hidden = m.start() > 0
        lines[i] = line[m.start():]
        j = i - 1
        while j >= 0 and i - j <= 3 and lines[j] is not None and len(lines[j]) <= 128 \
                and not OWN_NOTE.match(lines[j]) and not PASSWORD_PROMPT.search(lines[j]):
            lines[j], hidden = None, True
            j -= 1
        if hidden:
            lines[i] = TYPED_AHEAD_SAYS + lines[i]
    return "\n".join(line for line in lines if line is not None)


def clean(report, scrub):
    """A report as it may leave the machine: every string scrubbed, and a manual update's journal (or one
    switched to manual with Ctrl+]) without what was typed ahead of a password prompt."""
    report = scrub_all(report, scrub)
    if isinstance(report, dict) and isinstance(report.get("log"), str):
        update = report.get("update") if isinstance(report.get("update"), dict) else {}
        if update.get("mode") == "manual" or SWITCHED_TO_MANUAL.search(report["log"]):
            report["log"] = hide_typed_ahead(report["log"])
    return report


def scrub_all(obj, scrub):
    """Every string in a report, keys included."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_all(v, scrub) for v in obj]
    if isinstance(obj, dict):
        return {scrub(str(k)): scrub_all(v, scrub) for k, v in obj.items()}
    return obj


# ───────────────────────────── the machine ─────────────────────────────

def _read(path, limit=200):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def _driver(dev):
    try:
        return os.path.basename(os.readlink(os.path.join(dev, "driver")))
    except OSError:
        return ""


def _pci_names(ids):
    """{"8086:9b41": "Intel … UHD Graphics"} from hwdata's pci.ids, for the few ids asked about."""
    want = {i.split(":")[0]: set() for i in ids}
    for i in ids:
        want[i.split(":")[0]].add(i.split(":")[1])
    names, vendor, vname = {}, None, ""
    try:
        with open("/usr/share/hwdata/pci.ids", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                if not line.startswith("\t"):
                    vendor, _, vname = line.strip().partition("  ")
                    vendor = vendor.lower()
                elif vendor in want and not line.startswith("\t\t"):
                    dev, _, dname = line.strip().partition("  ")
                    if dev.lower() in want[vendor]:
                        names[f"{vendor}:{dev.lower()}"] = f"{vname} {dname}"
    except OSError:
        pass
    return names


def _gpus():
    found = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        dev = os.path.join(card, "device")
        if "-" in os.path.basename(card) or not os.path.isdir(dev):    # card0-HDMI-A-1: a connector
            continue
        vendor = _read(os.path.join(dev, "vendor")).replace("0x", "")
        device = _read(os.path.join(dev, "device")).replace("0x", "")
        found.append((f"{vendor}:{device}" if vendor else "", _driver(dev)))
    names = _pci_names([i for i, _ in found if i])
    out = []
    for i, drv in found:
        out.append({"id": i, "name": names.get(i, ""), "driver": drv})
    return out


def _network():
    """How the machine is online: through which kind of link, adapter and driver, with what signal.
    Never the network's name, the addresses or the interface's MAC."""
    iface = ""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[1] == "00000000":
                    iface = parts[0]
                    break
    except OSError:
        pass
    if not iface:
        return {"type": "нет сети"}
    sysdir = f"/sys/class/net/{iface}"
    wifi = os.path.isdir(os.path.join(sysdir, "wireless")) or os.path.exists(os.path.join(sysdir, "phy80211"))
    dev = os.path.join(sysdir, "device")
    net = {"type": "Wi-Fi" if wifi else ("кабель" if os.path.isdir(dev) else "другое"), "driver": _driver(dev)}
    if os.path.isdir(dev):
        vendor = _read(os.path.join(dev, "vendor")).replace("0x", "")
        device = _read(os.path.join(dev, "device")).replace("0x", "")
        if vendor and device:
            net["adapter"] = _pci_names([f"{vendor}:{device}"]).get(f"{vendor}:{device}", f"{vendor}:{device}")
    if wifi:
        try:
            with open("/proc/net/wireless") as f:
                for line in f.readlines()[2:]:
                    parts = line.split()
                    if parts and parts[0].rstrip(":") == iface and len(parts) > 3:
                        net["signal_dbm"] = int(float(parts[3].rstrip(".")))
        except (OSError, ValueError):
            pass
    else:
        speed = _read(os.path.join(sysdir, "speed"))
        if speed.isdigit():
            net["speed_mbps"] = int(speed)
    return net


def _terminal():
    named = os.environ.get("ARCH_UPDATE_TERMINAL_USED") or os.environ.get("TERM_PROGRAM", "")
    if named:
        return named
    for var, name in (("KONSOLE_VERSION", "konsole"), ("KITTY_WINDOW_ID", "kitty"), ("ALACRITTY_WINDOW_ID", "alacritty"),
                      ("WEZTERM_EXECUTABLE", "wezterm"), ("PTYXIS_VERSION", "ptyxis"), ("VTE_VERSION", "vte")):
        if os.environ.get(var):
            return name
    return os.environ.get("TERM", "")


def _free_gib(path):
    try:
        return round(shutil.disk_usage(path).free / 2 ** 30, 1)
    except OSError:
        return None


def _version_of(*pkgs):
    for p in pkgs:
        out = engine.run(["pacman", "-Q", p], timeout=10).split()
        if len(out) == 2:
            return out[1]
    return ""


def device(ui=None):
    """What this machine is, without anything that says whose it is."""
    dmi = {k: _read(f"/sys/class/dmi/id/{k}") for k in ("sys_vendor", "product_name", "product_version",
                                                       "product_family", "chassis_type")}
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "Hardware", "Model") and value.strip():
                    cpu = re.sub(r"\s+", " ", value.strip())
                    break
    except OSError:
        pass
    mem = 0
    try:
        with open("/proc/meminfo") as f:
            mem = int(f.readline().split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    os_release = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                os_release[k] = v.strip('"')
    except OSError:
        pass
    bat = engine.battery()
    d = {
        "vendor": dmi["sys_vendor"], "model": dmi["product_name"], "model_version": dmi["product_version"],
        "family": dmi["product_family"], "chassis": dmi["chassis_type"], "chromebook": engine.is_chromebook(),
        "cpu": cpu, "cores": os.cpu_count(), "ram_gib": round(mem / 2 ** 30, 1) if mem else None,
        "gpu": _gpus(), "nvidia": _read("/sys/module/nvidia/version"),
        "arch": platform.machine(), "kernel": platform.release(), "distro": os_release.get("ID", ""),
        "kernels": sorted(engine.kernel_packages()), "secure_boot": engine.secure_boot_on(),
        "boot_readable": os.access("/boot", os.X_OK),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""), "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "terminal": _terminal(), "lang": os.environ.get("LANG", "")[:2],
        "battery": {"percent": bat[0], "discharging": bat[1]} if bat else None,
        "network": _network(), "free_gib": {"/": _free_gib("/"), "/boot": _free_gib("/boot")},
        "python": platform.python_version(), "pacman": _version_of("pacman"),
        "arch_update": _version_of("arch-update-bin", "arch-update"),
        "aur_helper": next((h for h in ("yay", "paru", "pikaur") if shutil.which(h)), ""),
    }
    if ui:
        d.update({k: ui[k] for k in ("screen", "monitors") if k in ui})
    return d


def updater_version():
    """{"commit", "date"} of this checkout of the updater."""
    out = engine.run(["git", "-C", ROOT, "log", "-1", "--format=%h%x09%cd", "--date=format:%d.%m.%Y"],
                     timeout=3).strip().split("\t")
    return {"commit": out[0], "date": out[1]} if len(out) == 2 else {}


# ───────────────────────────── the report ─────────────────────────────

def _log_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, TypeError):
        return ""
    if len(text) > LOG_LIMIT:
        head, tail = _cut(text)
        text = f"{head}\n…[пропущено {len(text) - len(head) - len(tail)} символов]…\n{tail}"
    return text


def _cut(text):
    """The head (LOG_LIMIT // 4) and the tail (the rest of LOG_LIMIT) of a longer log, cut between lines,
    or in a line that long between words: a name cut in two would get past the scrubber. Only a word
    that long is cut where it falls."""
    end, start = LOG_LIMIT // 4, len(text) - LOG_LIMIT * 3 // 4
    for sep in "\n ":
        i = text.rfind(sep, 0, end)
        if i != -1:
            end = i
            break
    last = text.rfind("\n", 0, end) + 1
    if TYPED_AHEAD.match(text, last):                     # a password typed ahead, its prompt cut away
        end = max(last - 1, 0)
    for sep in "\n ":
        i = text.find(sep, start, len(text) - 1)          # not the newline that ends the log
        if i != -1:
            start = i + 1
            break
    return text[:end], text[start:]


def build(res, mode="auto", seconds=None, ui=None, scrub=None):
    """The report of one update, with everything personal taken out (by scrub, scrubber()'s by default)."""
    report = {
        "schema": SCHEMA, "id": report_id(),
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updater": updater_version(), "device": device(ui),
        "update": {"mode": mode, "verdict": res.get("verdict"), "started": res.get("started", True),
                   "exit": res.get("exitstatus"), "seconds": round(seconds) if seconds else None,
                   "changed": res.get("changed", []), "added": res.get("added", []),
                   "removed": res.get("removed", []), "problems": res.get("problems", []),
                   "warnings": res.get("warnings", []), "notes": res.get("notes", []), "ok": res.get("ok", [])},
        "log": _log_text(res.get("log")),
    }
    return clean(report, scrub or scrubber())


def _prune(folder):
    """Only the newest KEEP[folder] reports stay."""
    for old in sorted(glob.glob(os.path.join(folder, "*.json")))[:-KEEP[folder]]:
        _remove(old)
    _drop_old_tmp(folder)


def _drop_old_tmp(folder):
    """Temp files a write left behind (killed halfway): gone after a day, when no write can still be at it."""
    day_ago = time.time() - 86400
    for tmp in glob.glob(os.path.join(folder, "*.tmp")):
        try:
            if os.path.getmtime(tmp) < day_ago:
                os.remove(tmp)
        except OSError:
            pass


def queue(report):
    os.makedirs(OUTBOX, exist_ok=True)
    path = os.path.join(OUTBOX, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2) + ".json")
    _write_json(path, report)
    _prune(OUTBOX)
    return path


def start_sender():
    """Send in a process of its own: it finishes even if the window closes right away."""
    try:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "--send"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def after_update(res, demo=False, **info):
    """With consent (and never in the demo): queue this update's report and send it in the background.
    Returns the queued file, or None. Never raises: a report must not get in the way of anything."""
    if demo or not res or not enabled():
        return None
    try:
        if not report_id():
            set_consent(True)                       # an ID for a choice made by hand in settings.json
        report = build(res, **info)
        path = queue(report)
    except Exception:
        return None
    # The update's record: its report waits as this file and goes out with this time. One sent by hand later
    # takes the same time, so that the relay knows it for the same report and files no second issue.
    name = os.path.basename(_journal(res.get("log")) or "")
    if name:
        _record(name, queued=os.path.basename(path), time=report["time"])
    start_sender()
    return path


def _note(**kw):
    s = _read_json(STATUS)
    s.update(kw)
    try:
        _write_json(STATUS, s)
    except OSError:
        pass


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def send_pending(timeout=15):
    """Send the outbox, oldest first. Returns (sent, still waiting)."""
    waiting = sorted(glob.glob(os.path.join(OUTBOX, "*.json")))
    url = endpoint()
    if not waiting or not url or not enabled():
        return 0, len(waiting)
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "send.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0, len(waiting)                     # another sender is at it
        _drop_old_tmp(REPORTS)
        return _send(waiting, url, timeout)


def _tries(names):
    """How often the relay failed on each report still waiting, as status.json counts them."""
    got = _read_json(STATUS).get("tries")
    if not isinstance(got, dict):
        return {}
    return {k: n for k, n in got.items() if k in names and type(n) is int and n > 0}


def _agent():
    return "arch-update-cactus/" + updater_version().get("commit", "?")


def _post(url, report, timeout, agent):
    """One report to the relay: (HTTP status, its answer as a dict: {"ok": true, "issue": 12}, or {}). Raises
    OSError, ValueError or http.client.HTTPException when no answer came (offline, DNS, TLS, a garbled one)."""
    data = json.dumps(report, ensure_ascii=False).encode("utf-8", "replace")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _answer(r)
    except urllib.error.HTTPError as e:
        return e.code, _answer(e)


def _answer(r):
    """The JSON the relay answers with; {} for a body that is none (cut off, or no JSON at all)."""
    try:
        got = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _issue_of(d):
    n = d.get("issue") if isinstance(d, dict) else None
    return n if type(n) is int and n > 0 else None


def _send(waiting, url, timeout):
    sent = 0
    agent = _agent()
    scrub = scrubber()
    tries = _tries({os.path.basename(p) for p in waiting})
    unsure = {}                                         # name → path: 502/504 on it, on it alone or on every report?
    for path in waiting:
        if not enabled():                               # switched off meanwhile
            break
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except OSError:                                 # taken back meanwhile
            continue
        except ValueError:
            report = None
        if not isinstance(report, dict):                # damaged: the relay would refuse it
            _file_away(path, REJECTED)
            continue
        # Through the scrubber once more: one queued by an older version goes out as clean as a new one.
        report = clean(report, scrub)
        try:
            code, answer = _post(url, report, timeout, agent)
        except (OSError, ValueError, http.client.HTTPException) as e:   # offline, DNS, TLS: try again later
            _note(last_error=f"{type(e).__name__}: {e}"[:200], tries=tries)
            break
        if 200 <= code < 300:
            sent += 1
            tries.pop(name, None)
            _note(last_sent=_now(), last_error="", tries=tries)
            _went_out(name, _issue_of(answer))          # before it leaves the outbox: never "not sent" between
            _file_away(path, SENT, report)
        elif code in (404, 405, 408, 429, 503):         # not set up, busy or limited: the others would fare alike
            _note(last_error=f"HTTP {code}", tries=tries)
            break
        elif code in (502, 504):                        # GitHub refused or did not answer: an expired token fails
            unsure[name] = (path, code)                 # every report alike, so only a report that goes through
            _note(last_error=f"HTTP {code}", tries=tries)   # later in this round shows it was this one
            if len(unsure) >= 2 and not sent:
                break
        elif code >= 500:                               # the relay failed on this one: the next may go through
            _failed_try(name, path, code, tries, report)
        else:                                           # refused for good: keep a few to look at
            tries.pop(name, None)
            _note(last_error=f"HTTP {code}: отчёт не принят", tries=tries)
            _file_away(path, REJECTED, report)
    if sent:                                            # the relay works: those failed on their own
        for name, (path, code) in unsure.items():
            _failed_try(name, path, code, tries, None, quiet=True)
    return sent, len(glob.glob(os.path.join(OUTBOX, "*.json")))


def _failed_try(name, path, code, tries, report, quiet=False):
    """One more failed try for this report; after TRIES of them it is set aside, so it holds up no other.
    quiet: counted after the round, when a later report went through: last_error stays as that left it."""
    tries[name] = tries.get(name, 0) + 1
    if tries[name] < TRIES:
        _note(tries=tries, **({} if quiet else {"last_error": f"HTTP {code}"}))
        return
    del tries[name]
    _note(last_error=f"HTTP {code}: отчёт не принят после {TRIES} попыток", tries=tries)
    _file_away(path, REJECTED, report)


def _file_away(path, dest, report=None):
    """Out of the outbox into dest (sent/ or rejected/): the report as it went out, or the file itself
    when there is none or it cannot be written."""
    target = os.path.join(dest, os.path.basename(path))
    copied = False
    if report is not None:
        try:
            _write_json(target, report)
            copied = True
        except OSError:
            pass
    try:
        if copied:
            os.remove(path)
        else:
            os.makedirs(dest, exist_ok=True)
            os.replace(path, target)
    except OSError:
        pass
    _prune(dest)


def state_of(path):
    """Where one queued report is now: "sent", "waiting", "rejected" or "" (gone)."""
    name = os.path.basename(path or "")
    for folder, word in ((SENT, "sent"), (OUTBOX, "waiting"), (REJECTED, "rejected")):
        if name and os.path.exists(os.path.join(folder, name)):
            return word
    return ""


def status():
    """For the settings screen: the same keys and types whatever status.json and settings.json hold."""
    s = _read_json(STATUS)
    return {"enabled": enabled(), "decided": decided(), "id": report_id(), "endpoint": bool(endpoint()),
            "waiting": len(glob.glob(os.path.join(OUTBOX, "*.json"))),
            "last_sent": _str(s.get("last_sent")), "last_error": _str(s.get("last_error"))}


def preview():
    """The last report that went out, or one put together now from the newest log, as text."""
    sent = sorted(glob.glob(os.path.join(SENT, "*.json")) + glob.glob(os.path.join(OUTBOX, "*.json")),
                  key=os.path.basename)
    report = _read_json(sent[-1]) if sent else {}
    if report:
        report = clean(report, scrubber())             # as it goes out (see _send)
    else:
        logs = sorted(glob.glob(os.path.join(engine.STATE_DIR, "logs", "20*.log")))
        report = build({"verdict": None, "log": logs[-1] if logs else None}, mode="—")
        report["id"] = report["id"] or "(появится, когда отчёты включены)"
    return json.dumps(report, ensure_ascii=False, indent=1)


# ───────────────────────────── the updates kept on this machine ─────────────────────────────

JOURNAL = engine.JOURNAL_NAME                                              # engine.RunLog's names
QUEUED = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}\.json")                     # queue()'s names
RELAY_TIME = re.compile(r"20[2-9]\d-\d\d-\d\dT\d\d:\d\d:\d\dZ")            # what the relay takes: 2020…2099, UTC
# The updater's own lines in a journal, read as the engine reads them (engine.journal_events): of a very long one
# (a long AUR build) its head and the rest of WHOLE from its end, where the result follows КОНЕЦ.
WHOLE = engine.JOURNAL_WHOLE


def _journals():
    """The names of the journals in logs/, newest first."""
    try:
        names = os.listdir(LOGS)
    except OSError:
        return []
    paths = (os.path.join(LOGS, n) for n in names if JOURNAL.fullmatch(n) and _stamp(n))
    return sorted((os.path.basename(p) for p in paths if os.path.isfile(p) and not os.path.islink(p)), reverse=True)


def _stamp(name):
    """When the update of a journal began, from its name: local time. None for another name."""
    try:
        return datetime.datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")
    except (TypeError, ValueError):
        return None


def _journal(log):
    """The journal of an update of this machine that log names (its path, its name, or latest.log) as a path in
    logs/; None for anything else: a file outside logs/ (the demo's, in demo-logs/, too) or of another kind."""
    try:
        log = os.fspath(log)
    except TypeError:
        return None
    if not isinstance(log, str) or not log or "\0" in log:
        return None
    path = os.path.abspath(log) if os.sep in log else os.path.join(LOGS, log)
    try:
        if os.path.islink(path):                   # latest.log: the newest journal
            path = os.path.realpath(path)
        if os.path.realpath(os.path.dirname(path)) != os.path.realpath(LOGS):
            return None
    except (OSError, ValueError):
        return None
    name = os.path.basename(path)
    return os.path.join(LOGS, name) if JOURNAL.fullmatch(name) and _stamp(name) else None


def _local(value):
    """An ISO time as the engine writes it, as a local time without a zone; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return t.astimezone().replace(tzinfo=None) if t.tzinfo else t
    except (ValueError, OverflowError, OSError):
        return None


def _relay_time(t):
    """A local time as the relay takes it: UTC, "2026-10-30T17:45:45Z", 2020…2099. Now for one it would not take."""
    try:
        utc = t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if RELAY_TIME.fullmatch(utc):
            return utc
    except (AttributeError, ValueError, OverflowError, OSError):
        pass
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strs(value):
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _blank(began):
    return {"verdict": None, "started": True, "mode": None, "exitstatus": None, "changed": [], "added": [],
            "removed": [], "problems": [], "warnings": [], "notes": [], "ok": [], "began": began, "ended": None,
            "resolved": False}


def _from_summary(path, began):
    """What the engine's summary next to a journal (<stamp>.json) says of its update; None without a usable one."""
    d = _read_json(path[:-4] + ".json") if path.endswith(".log") else {}
    verdict = d.get("verdict", "")
    if not (verdict is None or (type(verdict) is int and verdict in (0, 1, 2))):
        return None
    u = _blank(_local(d.get("began")) or began)
    for key in ("changed", "added", "removed", "problems", "warnings", "notes", "ok"):
        u[key] = _strs(d.get(key))
    code = d.get("exitstatus")
    u.update(verdict=verdict, started=d.get("started") is not False, ended=_local(d.get("ended")),
             mode=d.get("mode") if d.get("mode") in ("auto", "manual") else None,
             exitstatus=code if type(code) is int else None, resolved=bool(d.get("resolved")))
    return u


def _from_journal(path, began):
    """What a journal says of its update in the updater's own lines, the same in the journals of every version: the
    mode (СТАРТ), the exit code (КОНЕЦ), a run that never started (НЕ НАЧАТО), then the checks (ПАКЕТЫ, ПРОБЛЕМА,
    ВНИМАНИЕ, as system_checks writes them), and a fix that went through afterwards (ИСПРАВЛЕНО, for one whose summary
    could not be written). No checks, no verdict: the update was cut off."""
    u = _blank(began)
    try:
        text = engine.journal_text(path)
    except OSError:
        return u
    events = engine.journal_events(path, text)
    u["resolved"] = bool(engine.RESOLVED_LINE.search(text))
    checked = False
    day, last = began.date(), began.hour * 3600 + began.minute * 60 + began.second
    for hh, mm, ss, kind, text in events:
        h, m, s = int(hh), int(mm), int(ss)
        if h > 23 or m > 59 or s > 59:
            continue
        if h * 3600 + m * 60 + s < last - 3 * 3600:          # past midnight (a clock put back an hour is not)
            day += datetime.timedelta(days=1)
        last = h * 3600 + m * 60 + s
        u["ended"] = datetime.datetime.combine(day, datetime.time(h, m, s))
        text = text.rstrip()
        if kind == "СТАРТ":
            u["mode"] = "manual" if "режим: ручной" in text else "auto" if "режим: автоответы" in text else u["mode"]
        elif kind == "КОНЕЦ":
            code = re.search(r"код (-?\d+)", text)
            u["exitstatus"] = int(code.group(1)) if code else None
        elif kind == "НЕ НАЧАТО":                   # engine.not_started: only a boot partition not mounted is a problem
            u["started"] = False
            u["verdict"] = 2 if "не смонтирован" in text else 1
            (u["problems"] if u["verdict"] == 2 else u["warnings"]).append(text)
        elif kind == "ПАКЕТЫ" and text.startswith("обновлены:"):     # the checks' first line: "name old→new, …"
            checked = True
            u["changed"] = [p.split(" ", 1)[0] for p in _items(text[len("обновлены:"):])]
        elif kind == "ПАКЕТЫ":
            lists = re.fullmatch(r"новые: (.*); удалены: (.*)", text)
            if lists:
                u["added"], u["removed"] = _items(lists.group(1)), _items(lists.group(2))
        elif kind == "ПРОБЛЕМА":
            u["problems"].append(text)
        elif kind == "ВНИМАНИЕ":
            u["warnings"].append(text)
    if checked and u["started"]:
        u["verdict"] = 2 if u["problems"] else 1 if u["warnings"] else 0
    return u


def _items(text):
    """A list as the engine writes it in its lines: "a, b" or "—" for none."""
    return [p for p in text.strip().split(", ") if p not in ("", "—")]


def _update_of(path):
    """What is known of one update: its summary if the engine left one, else what its journal says."""
    began = _stamp(os.path.basename(path)) or datetime.datetime.now().replace(microsecond=0)
    u = _from_summary(path, began) or _from_journal(path, began)
    took = (u["ended"] - u["began"]).total_seconds() if u["ended"] else -1
    u["seconds"] = took if 0 <= took <= 30 * 86400 else None
    return u


def local_updates(n=SHOWN):
    """The newest n updates kept on this machine (logs/, never the demo's), newest first. Each: {"log": its journal,
    "time": when it began (ISO, local), "verdict": 0, 1, 2 or None (no result: the update was cut off), "packages":
    how many changed, "problems", "warnings", "notes": lists of text, "sent": when its report went out (ISO, local;
    "" if it never did), "issue": the issue it became, or None}; besides: "ended" (ISO or ""), "started", "mode",
    "exitstatus", "changed", "added", "removed", "ok", "resolved", "seconds" and "waiting" (its automatic report
    waits in the outbox). From the summary next to the journal, or, for an update from before there were summaries,
    from the journal's own lines."""
    names = _journals()
    names = names[:n] if n is None or n > 0 else []
    records = _records()
    auto = _auto_reports(records)
    out = []
    for name in names:
        path = os.path.join(LOGS, name)
        try:
            u = _update_of(path)
        except Exception:                           # one journal the list cannot read is listed all the same
            u = dict(_blank(_stamp(name)), seconds=None)
        rec = records.get(name, {})
        sent, waiting = _sent_of(rec, auto.get(name, []))
        out.append({
            "log": path, "time": u["began"].isoformat(timespec="seconds"),
            "ended": u["ended"].isoformat(timespec="seconds") if u["ended"] else "",
            "verdict": u["verdict"], "started": u["started"], "mode": u["mode"], "exitstatus": u["exitstatus"],
            "packages": len(u["changed"]) + len(u["added"]) + len(u["removed"]),
            "changed": u["changed"], "added": u["added"], "removed": u["removed"], "problems": u["problems"],
            "warnings": u["warnings"], "notes": u["notes"], "ok": u["ok"], "resolved": u["resolved"],
            "seconds": u["seconds"], "sent": sent, "issue": _issue_of(rec), "waiting": waiting,
        })
    return out


def _sent_of(rec, reports):
    """(when its report went out, "" if never; whether its automatic report still waits) of one update: its record,
    or its automatic report in sent/ or the outbox (one the version before this one sent, which kept no record)."""
    sent = _str(rec.get("sent"))
    if not sent:
        for folder, q in reports:
            if folder == SENT:
                try:
                    sent = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(SENT, q))).isoformat(
                        timespec="seconds")
                    break
                except (OSError, ValueError, OverflowError):
                    pass
    return sent, not sent and any(folder == OUTBOX for folder, _ in reports)


def report_state(log):
    """{"sent", "issue", "waiting"} of one update's report, as local_updates says them: for the menu's warning."""
    name = os.path.basename(_journal(log) or "")
    rec = _records().get(name, {}) if name else {}
    sent, waiting = _sent_of(rec, _auto_reports().get(name, [])) if name else ("", False)
    return {"sent": sent, "issue": _issue_of(rec), "waiting": waiting}


def _queued_at(name):
    """When queue() queued a report, from its name (local time); None for another name."""
    try:
        return datetime.datetime.strptime(name[:15], "%Y%m%d-%H%M%S") if QUEUED.fullmatch(name) else None
    except ValueError:
        return None


def _auto_reports(records=None):
    """The automatic reports kept on this machine, waiting, sent or refused, by the journal of their update:
    {journal's name: [(folder, report's name)]}. after_update links the two in manual.json; a report from before
    there were records (it was queued as its update ended) is the newest journal's that began before it, within a
    day."""
    records = _records() if records is None else records
    linked = {_str(r.get("queued")): name for name, r in records.items() if _str(r.get("queued"))}
    starts = sorted((_stamp(n), n) for n in _journals())
    out = {}
    for folder in (OUTBOX, SENT, REJECTED):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for q in sorted(names):
            at = _queued_at(q)
            if at is None:
                continue
            owner = linked.get(q)
            if owner is None:
                before = [n for began, n in starts if began <= at]
                owner = before[-1] if before and at - _stamp(before[-1]) <= datetime.timedelta(days=1) else None
            if owner:
                out.setdefault(owner, []).append((folder, q))
    return out


def _report_time(name):
    """The time the relay knows this update's report by, if one of its reports went out already (or is on its way):
    from its record, else from its automatic report itself. None when there is none."""
    known = _records().get(name, {}).get("time")
    if isinstance(known, str) and RELAY_TIME.fullmatch(known):
        return known
    for folder, q in _auto_reports().get(name, []):
        t = _read_json(os.path.join(folder, q)).get("time")
        if isinstance(t, str) and RELAY_TIME.fullmatch(t):
            return t
    return None


def build_from_log(log, ui=None):
    """The report of one update kept on this machine, put together now from its summary or its journal, everything
    personal taken out as build() does, the names the journal itself shows too (an account or a machine renamed
    since). Its time is the update's own, when it ended, or the one its report had already: the same update sent
    again is the same report to the relay, which files no second issue for it. The machine and the updater are
    described as they are now, and the log says so at its top."""
    u = _update_of(log)
    res = {k: u[k] for k in ("verdict", "started", "exitstatus", "changed", "added", "removed", "problems",
                             "warnings", "notes", "ok")}
    res["log"] = log
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            users, hosts, homes = names_in(f.read())
    except (OSError, TypeError):
        users, hosts, homes = set(), set(), set()
    report = build(res, mode=u["mode"], seconds=u["seconds"], ui=ui, scrub=scrubber(users, hosts, homes))
    if u["mode"] is None and not SWITCHED_TO_MANUAL.search(report["log"]):
        report["log"] = hide_typed_ahead(report["log"])      # no telling how it ran: as if by hand (see clean)
    report["time"] = _report_time(os.path.basename(_journal(log) or "")) or _relay_time(u["ended"] or u["began"])
    report["log"] = (f"[отчёт отправлен вручную {datetime.datetime.now():%d.%m.%Y %H:%M}; устройство и версия "
                     "апдейтера в нём — на это время]\n" + report["log"])
    return report


def send_now(log, timeout=20, ui=None):
    """The report of one update kept on this machine, sent now because the user asked for it (a key on the summary
    or in the list of the last updates): that key is the consent for this one report, with the automatic reports
    off too, and switches nothing on. A machine without a report ID gets one, once the report went out. Allowed
    again for a report that went out already. Returns (sent, what to tell the user, the issue's number or None);
    never raises."""
    try:
        return _send_now(log, timeout, ui)
    except Exception:
        return False, "не удалось собрать отчёт", None


def _send_now(log, timeout, ui):
    path = _journal(log)
    if not path:
        return False, "это не журнал обновления этого компьютера", None
    if not os.path.isfile(path):
        return False, "журнал этого обновления не найден", None
    url = endpoint()
    if not url:
        return False, "сервер для отчётов не подключён", None
    rid = ensure_id(save=False)                      # saved once the report went out with it
    report = build_from_log(path, ui=ui)
    report["id"] = report["id"] or rid
    try:
        code, answer = _post(url, report, timeout, _agent())
    except (OSError, ValueError, http.client.HTTPException):         # offline, DNS, TLS, a garbled answer
        return False, "нет связи с сервером — попробуй позже", None
    if 200 <= code < 300:
        if report["id"] == rid:
            ensure_id(save=True)
        name = os.path.basename(path)
        rec = _records().get(name, {})
        again = answer.get("duplicate") is True                       # the relay has this report already
        issue = _issue_of(answer) or _issue_of(rec)                   # the same time: the same log, the same issue
        _record(name, sent=_now(), issue=issue, time=report["time"])
        for folder, q in _auto_reports().get(name, []):
            if folder == OUTBOX:
                _remove(os.path.join(OUTBOX, q))     # this update's automatic report, still waiting: it went out now
        number = f" (№ {issue})" if issue else ""
        return True, (f"этот отчёт уже у разработчика{number}" if again else
                      f"отчёт отправлен разработчику{number}"), issue
    if code == 429:
        return False, f"сервер просит подождать — попробуй позже: до {PER_HOUR} отчётов в час с компьютера", None
    if code == 408:
        return False, "нет связи с сервером — попробуй позже", None
    if code >= 500:
        return False, f"сервер не принял отчёт (HTTP {code}) — попробуй позже", None
    return False, f"сервер не принял отчёт (HTTP {code})", None


def delete_local(log):
    """One update deleted from this machine: its journal (.log, .raw), its summary (.json), its record and its
    automatic report wherever that is (waiting, sent or refused: «Что именно уходит» shows it no more). Only an
    update in logs/, not the one running now, and not the one the menu warns about (engine.unresolved: it found
    problems in this boot, nothing has fixed them — without it the menu would stop saying not to reboot): anything
    else is refused (False). True once all of it is gone; never raises."""
    try:
        path = _journal(log)
        if not path or (engine.RUNNING and engine.LOG and os.path.realpath(engine.LOG.path) == os.path.realpath(path)):
            return False
        if is_unresolved(path):
            return False
        name = os.path.basename(path)
        reports = _auto_reports().get(name, [])
        gone = True
        for f in (path, path[:-4] + ".raw", path[:-4] + ".json"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
            except OSError:
                gone = False
        for folder, q in reports:
            _remove(os.path.join(folder, q))
        _record(name, forget=True)
        _point_latest()
        return gone
    except Exception:
        return False


def is_unresolved(log):
    """The update of that journal is the one the menu warns about: it found problems in this boot, unfixed."""
    try:
        bad = engine.unresolved(LOGS)
        return bool(bad) and os.path.realpath(bad["log"]) == os.path.realpath(log)
    except Exception:                               # no telling: better kept than the warning lost
        return True


def _point_latest():
    """latest.log, when the journal it named is gone: to the newest one left, or away."""
    latest = os.path.join(LOGS, "latest.log")
    if not os.path.islink(latest) or os.path.exists(latest):
        return
    left = _journals()
    tmp = f"{latest}.{os.getpid()}.tmp"
    try:
        if left:
            os.symlink(left[0], tmp)
            os.replace(tmp, latest)
        else:
            os.remove(latest)
    except OSError:
        _remove(tmp)


def _records():
    """manual.json: {journal's name: {"sent": when its report went out (ISO, local), "issue": its number, "time": the
    report's time, "queued": its automatic report in the outbox}}."""
    return {k: v for k, v in _read_json(RECORDS).items() if JOURNAL.fullmatch(k) and isinstance(v, dict)}


def _record(name, forget=False, **fields):
    """One journal's record changed, or forgotten; those of journals no longer kept go. Never raises."""
    try:
        with _locked():
            records = _records()
            if forget:
                records.pop(name, None)
            else:
                records.setdefault(name, {}).update(fields)
            kept = set(_journals())
            _write_json(RECORDS, {k: v for k, v in records.items() if k in kept})
    except OSError:
        pass


def _went_out(queued, issue):
    """An automatic report went out: the record of its update says so (after_update linked the two)."""
    try:
        with _locked():
            records = _records()
            for rec in records.values():
                if rec.get("queued") == queued:
                    rec.update(sent=_now(), issue=issue or _issue_of(rec))
                    _write_json(RECORDS, records)
                    return
    except OSError:
        pass


# ───────────────────────────── the command line ─────────────────────────────

def _plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _when(iso, year=True):
    t = _local(iso)
    return t.strftime("%d.%m.%Y %H:%M" if year else "%d.%m %H:%M") if t else "—"


def _verdict_words(u):
    if not u["started"]:
        return ("✗ " if u["verdict"] == 2 else "· ") + "не начиналось"
    if u["resolved"]:                               # it found problems, and a fix and a second look ended them
        return "✓ исправлено"
    return {0: "✓ всё чисто", 1: "⚠ есть что прочитать", 2: "✗ нужна проверка"}.get(u["verdict"], "· без итога")


def listing(n=SHOWN):
    """The last updates as report.py --list prints them."""
    updates = local_updates(n)
    if not updates:
        return "На этом компьютере пока нет журналов обновлений."
    lines = ["Последние обновления (1 — самое новое):"]
    for i, u in enumerate(updates, 1):
        count = u["packages"]
        known = u["started"] and u["verdict"] is not None
        packages = f"{count} {_plural(count, 'пакет', 'пакета', 'пакетов')}" if known else "—"
        if u["sent"]:
            sent = f"отправлен {_when(u['sent'], year=False)}" + (f" (№ {u['issue']})" if u["issue"] else "")
        else:
            sent = "ждёт отправки" if u["waiting"] else "не отправлен"
        lines.append(f"{i:>2}  {_when(u['time'])}  {_verdict_words(u):<21} {packages:<12} {sent}")
        worst = [p for p in u["problems"] if "НЕ перезагружайся" in p] + u["problems"] + u["warnings"]
        first = worst[0] if worst else ""
        if first:
            lines.append("    " + (first if len(first) <= 110 else first[:109] + "…"))
    me = os.path.realpath(__file__).replace(os.path.expanduser("~"), "~", 1)
    lines += ["", f"Отправить отчёт разработчику: python3 {me} --send N"]
    return "\n".join(lines)


def _send_cli(log):
    path = _journal(log)
    if path and os.path.isfile(path):
        print(f"Отправляю отчёт об обновлении {_stamp(os.path.basename(path)):%d.%m.%Y %H:%M}…", flush=True)
    ok, message, _ = send_now(log)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def main(argv):
    if argv[:1] == ["--consent"] and argv[1:2] in (["yes"], ["no"]):
        set_consent(argv[1] == "yes")
    elif argv == ["--decided"]:
        return 0 if decided() else 1
    elif argv == ["--status"]:
        print("включены" if enabled() else "выключены")
    elif argv == ["--send"]:
        send_pending()
    elif argv == ["--preview"]:
        print(preview())
    elif argv == ["--list"]:
        print(listing())
    elif argv[:1] == ["--send"] and len(argv) == 2:
        names = _journals()
        if not (re.fullmatch(r"[0-9]{1,6}", argv[1]) and 1 <= int(argv[1]) <= len(names)):
            print(f"нет обновления под номером {argv[1]} — номера показывает report.py --list", file=sys.stderr)
            return 2
        return _send_cli(os.path.join(LOGS, names[int(argv[1]) - 1]))
    elif argv[:1] == ["--send-log"] and len(argv) == 2:
        return _send_cli(argv[1])
    else:
        print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
