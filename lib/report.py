#!/usr/bin/env python3
"""Update reports, only with the user's consent: after each update a report of it goes to the
developer, so that a problem on any machine can be looked into without asking for the log.

A report holds the update's result (verdict, changed packages, the post-update checks), what the
machine is (model, CPU, GPU, memory, kernel, desktop, terminal, and how it is connected: Wi-Fi or
cable, the adapter, the signal) and the update's log. Taken out of all of it: the user's and the
host's names, home paths, IP and MAC addresses, e-mail addresses and the machine ID. The name of
the Wi-Fi network is never read. Nothing is collected while reports are off (the default), and
the demo never reports.

Everything stays in ~/.local/state/arch-update-auto: settings.json (the choice, a random report
ID), reports/outbox (waiting to be sent) and reports/sent (the last ones that went out).

  report.py --consent yes|no   record the user's choice (the installer's question)
  report.py --decided          exit 0 if the user has chosen either way
  report.py --status           one line for the installer: on or off
  report.py --send             send what waits in the outbox (started detached after an update)
  report.py --preview          what a report from this machine looks like now
"""
import datetime
import fcntl
import glob
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
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import engine  # noqa: E402

# Where reports go: a small relay that files them in the developer's private repository on GitHub
# (relay/). Empty until the relay is deployed: reports wait in the outbox meanwhile.
ENDPOINT = ""
SCHEMA = 1
SETTINGS = os.path.join(engine.STATE_DIR, "settings.json")
REPORTS = os.path.join(engine.STATE_DIR, "reports")
OUTBOX = os.path.join(REPORTS, "outbox")
SENT = os.path.join(REPORTS, "sent")
REJECTED = os.path.join(REPORTS, "rejected")
STATUS = os.path.join(REPORTS, "status.json")
KEEP = {OUTBOX: 20, SENT: 20, REJECTED: 5}
LOG_LIMIT = 400_000                 # characters of log in one report: the head and the tail of a longer one
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def settings():
    return _read_json(SETTINGS)


def enabled():
    return settings().get("reports") is True


def decided():
    return "reports" in settings()


def set_consent(on):
    """The user's choice. Switching off also drops what was waiting to be sent."""
    s = settings()
    s["reports"] = bool(on)
    s["reports_decided"] = datetime.date.today().isoformat()
    if on and not re.fullmatch(r"[0-9a-f]{8}", str(s.get("report_id", ""))):
        s["report_id"] = secrets.token_hex(4)          # random: not derived from anything on the machine
    _write_json(SETTINGS, s)
    if not on:
        for f in glob.glob(os.path.join(OUTBOX, "*.json")):
            try:
                os.remove(f)
            except OSError:
                pass
    return s


def report_id():
    return settings().get("report_id", "")


# ───────────────────────────── taking out what is personal ─────────────────────────────

# Words that are names often enough on Linux machines ("arch", "user") but are everywhere in an update
# log: replacing every one of them would ruin the log, and as a user or host name they say nothing.
COMMON = {"arch", "archlinux", "linux", "user", "admin", "root", "home", "test", "pc", "computer", "laptop",
          "desktop", "chromebook", "localhost", "penguin", "manjaro", "endeavouros", "cachyos", "garuda"}
MAC = re.compile(r"(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])")
IPV4 = re.compile(r"(?<![\w.:-])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
                  r"(?![\w.-])")
IPV6 = re.compile(r"(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}"
                  r"|(?:[0-9A-Fa-f]{1,4}:)+:(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4})(?![\w:.])")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){0,8}\.(?!(?:service|socket|timer|mount|"
                   r"target|path|slice|scope|device|swap|automount)\b)[A-Za-z]{2,24}\b")


def _who():
    """(user names, host names, home folders, machine ID) of this machine, to be taken out."""
    users, names, homes = set(), set(), set()
    try:
        pw = pwd.getpwuid(os.getuid())
        users.add(pw.pw_name)
        homes.add(pw.pw_dir)
        names.update(p for p in re.split(r"[\s,]+", pw.pw_gecos) if p)     # the real name, if one is set
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


def scrubber():
    """A function that takes everything personal out of a text (see the module's doc)."""
    users, hosts, names, homes, machine = _who()
    rules = []
    for h in sorted(homes, key=len, reverse=True):            # /home/cactus/... → ~/...
        rules.append((re.compile(r"(?<![\w.-])" + re.escape(h) + r"(?![\w.-])"), "~"))
    for u in users:
        for h in hosts:
            rules.append((re.compile(re.escape(u) + "@" + re.escape(h), re.I), "<user>@<host>"))
    for u in sorted(users, key=len, reverse=True):
        rules.append((re.compile(r"(?i)(password for |пароль для |/home/|user |пользовател[ья] )" + re.escape(u)
                                 + r"(?![\w.-])"), r"\1<user>"))
    for words, repl in ((users, "<user>"), (hosts, "<host>"), (names, "<name>")):
        for w in sorted(words, key=len, reverse=True):
            if len(w) >= 3 and w.lower() not in COMMON:
                rules.append((re.compile(r"(?<![\w.-])" + re.escape(w) + r"(?![\w.-])", re.I), repl))
    if re.fullmatch(r"[0-9a-f]{32}", machine):
        rules.append((re.compile(machine, re.I), "<machine-id>"))
    rules += [(MAC, "<mac>"), (EMAIL, "<email>"), (IPV4, "<ip>"), (IPV6, "<ip>")]

    def scrub(text):
        for rx, repl in rules:
            text = rx.sub(repl, text)
        return text
    return scrub


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
        head, tail = text[:LOG_LIMIT // 4], text[-(LOG_LIMIT * 3 // 4):]
        text = f"{head}\n…[пропущено {len(text) - len(head) - len(tail)} символов]…\n{tail}"
    return text


def build(res, mode="auto", seconds=None, ui=None):
    """The report of one update, with everything personal taken out."""
    report = {
        "schema": SCHEMA, "id": report_id(),
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updater": updater_version(), "device": device(ui),
        "update": {"mode": mode, "verdict": res.get("verdict"), "started": res.get("started", True),
                   "exit": res.get("exitstatus"), "seconds": round(seconds) if seconds else None,
                   "changed": res.get("changed", []), "added": res.get("added", []),
                   "removed": res.get("removed", []), "problems": res.get("problems", []),
                   "warnings": res.get("warnings", []), "ok": res.get("ok", [])},
        "log": _log_text(res.get("log")),
    }
    return scrub_all(report, scrubber())


def _prune(folder):
    for old in sorted(glob.glob(os.path.join(folder, "*.json")))[:-KEEP[folder]]:
        try:
            os.remove(old)
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
        path = queue(build(res, **info))
    except Exception:
        return None
    start_sender()
    return path


def _note(**kw):
    s = _read_json(STATUS)
    s.update(kw)
    try:
        _write_json(STATUS, s)
    except OSError:
        pass


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
        return _send(waiting, url, timeout)


def _send(waiting, url, timeout):
    sent = 0
    agent = "arch-update-cactus/" + updater_version().get("commit", "?")
    for path in waiting:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": agent})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except (OSError, ValueError) as e:              # offline, DNS, TLS: try again later
            _note(last_error=f"{type(e).__name__}: {e}"[:200])
            break
        if 200 <= code < 300:
            dest = SENT
            sent += 1
            _note(last_sent=datetime.datetime.now().isoformat(timespec="seconds"), last_error="")
        elif code in (408, 429) or code >= 500:
            _note(last_error=f"HTTP {code}")
            break
        else:                                           # refused for good: keep a few to look at
            dest = REJECTED
            _note(last_error=f"HTTP {code}: отчёт не принят")
        os.makedirs(dest, exist_ok=True)
        try:
            os.replace(path, os.path.join(dest, os.path.basename(path)))
        except OSError:
            pass
        _prune(dest)
    return sent, len(glob.glob(os.path.join(OUTBOX, "*.json")))


def state_of(path):
    """Where one queued report is now: "sent", "waiting", "rejected" or "" (gone)."""
    name = os.path.basename(path or "")
    for folder, word in ((SENT, "sent"), (OUTBOX, "waiting"), (REJECTED, "rejected")):
        if name and os.path.exists(os.path.join(folder, name)):
            return word
    return ""


def status():
    """For the settings screen."""
    s = _read_json(STATUS)
    return {"enabled": enabled(), "decided": decided(), "id": report_id(), "endpoint": bool(endpoint()),
            "waiting": len(glob.glob(os.path.join(OUTBOX, "*.json"))),
            "last_sent": s.get("last_sent", ""), "last_error": s.get("last_error", "")}


def preview():
    """The last report that went out, or one put together now from the newest log, as text."""
    sent = sorted(glob.glob(os.path.join(SENT, "*.json")) + glob.glob(os.path.join(OUTBOX, "*.json")),
                  key=os.path.basename)
    report = _read_json(sent[-1]) if sent else {}
    if not report:
        logs = sorted(glob.glob(os.path.join(engine.STATE_DIR, "logs", "20*.log")))
        report = build({"verdict": None, "log": logs[-1] if logs else None}, mode="—")
        report["id"] = report["id"] or "(появится, когда отчёты включены)"
    return json.dumps(report, ensure_ascii=False, indent=1)


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
    else:
        print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
