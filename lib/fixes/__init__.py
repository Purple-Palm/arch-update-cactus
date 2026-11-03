"""The fixes catalogue: what Arch-Update can do by itself about a problem an update left, one module per fix in
this folder (F on the summary and under the menu's warning runs them).

Every fix goes the same way, in one root shell (RUNNER, one password):
  backup   copies of every file it may change, in /tmp (and room for them checked first): nothing is touched
           before they are made
  apply    the fix itself
  verify   a look at the result: the files it made are whole and right (an initramfs is built for its kernel and
           has its modules in it, a module list is there, a service is mounted…)
  rollback when apply or verify fails: every kept file put back as it was and checked against its copy, so a
           fix that did not work leaves the machine exactly as it found it. When even that fails, the copies stay
           in /tmp, the fix says where and prints the commands that put each one back by hand.
After the root shell, the engine looks at the boot files again (engine.recheck_boot): the update's warning goes
only when that look finds nothing wrong.

A fix is for the whole machine, or for one kernel (its scope: the kernel's package). Its id says which: the key
("depmod"), or the key and the package ("initramfs@linux"; a package name may hold an "@" too, a key never does: an
id splits at its first "@"). A fix that was not done holds up what comes after it: one for the whole machine every
fix after it, one for a kernel only the fixes after it for that same kernel (an initramfs of linux-lts that cannot
be built does not keep the one of linux broken).

A fix module has:
  KEY      its name: in the runner (the bash functions <KEY>_backup, <KEY>_apply, <KEY>_verify and, when putting
           the kept files back is not the way to undo it, <KEY>_rollback), in the ids, the result file and the
           journal
  SCRIPT   those bash functions; they may use the helpers of RUNNER (step, good, warn, bad, keep, keep_missing,
           kept_same, room_for, put_back, tmps_of, drop_tmps, ran_full, image_ok, image_trouble, nonempty,
           free_bytes, size, drain) and set failed_as to end without a rollback ("notenough": it did what it could,
           "incomplete": done but not whole, "unchanged": nothing was changed), done_as for a success of another
           kind ("partial"), and add words to note_extra for the result file (a reason, the mount point that ran
           full)
  plan(ctx) what it would do about the result in ctx: a list of fix() dicts, [] when it does not apply
The order of CATALOGUE is the order they run in: the space first (the others need it), then what the next one
builds on (the module lists before the initramfs that holds the modules, the initramfs before its signature).

A new fix: a module here, added to CATALOGUE, with tests in tests/test_fixes.py (apply, a failed apply, a failed
verify and the rollback, against stand-in commands). Its texts are Russian; its bash runs as root, so it gets
everything as arguments (never pasted into the script) and checks what it got.
"""

import os
import re
import tempfile
import traceback

import engine

RESULT_STATES = {
    "ok": "исправлено и проверено",
    "partial": "места хватило для исправлений, но для обновления его ещё мало",
    "rolledback": "не помогло — вернул всё как было",
    "rollbackfailed": "не помогло, и вернуть как было удалось не всё",
    "nobackup": "не начато — не смог сохранить копии, ничего не менял",
    "refused": "не начато — сейчас нельзя, ничего не менял",
    "notenough": "места на диске всё ещё мало",
    "incomplete": "файл ядра на месте, но mkinitcpio сообщил об ошибках — initramfs может быть неполным",
    "unchanged": "pacman ничего не поставил — ничего не менял",
    "skipped": "не делал (не прошло исправление перед ним)",
    "unknown": "этой версии оно незнакомо",
}
DONE = ("ok", "partial")
# The runner's exit status: 0 every fix done and checked, else the status of the first one that was not:
OK, ROLLED_BACK, ROLLBACK_FAILED, NO_BACKUP, REFUSED, NOT_ENOUGH, INCOMPLETE, UNCHANGED = 0, 80, 81, 82, 83, 84, 85, 86
SUDO_REFUSED = 125          # engine.ASK_SUDO_FIRST: the password was refused or the question cancelled
BY_STATUS = {OK: "ok", ROLLED_BACK: "rolledback", ROLLBACK_FAILED: "rollbackfailed", NO_BACKUP: "nobackup",
             REFUSED: "refused", NOT_ENOUGH: "notenough", INCOMPLETE: "incomplete", UNCHANGED: "unchanged"}
NAME = re.compile(r"[\w.+@-]+")          # a package or a DKMS module
KVER = re.compile(r"[\w.+~-]+")          # a kernel version
KEPT = re.compile(r"/tmp/arch-update-fix\.[A-Za-z0-9]{8}")   # the runner's folder for the copies (mktemp)
BOOT_TARGETS = ("/boot", "/efi", "/boot/efi")
GIB = 2 ** 30


def path_ok(p):
    return isinstance(p, str) and p.startswith("/") and "\n" not in p and "\0" not in p and ".." not in p.split("/")


def fix(key, label, argv, args, how=None, scope=None):
    """One planned fix: key, id (key, or key@scope for a fix of one kernel: scope is that kernel's package), label
    (for F and the journal), argv (the same by hand, one command), how (the same by hand as a shell line: what
    arch-update-auto prints), args (for the runner)."""
    return {"key": key, "id": key if scope is None else f"{key}@{scope}", "label": label, "argv": list(argv),
            "how": how or " ".join(_quote(a) for a in argv), "args": [str(a) for a in args]}


def _quote(a):
    return a if re.fullmatch(r"[\w./:=@+-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def scope_of(fix_id):
    """The package a fix's id is for (None: the whole machine)."""
    _, at, scope = str(fix_id).partition("@")
    return scope if at else None


class Context:
    """What the fixes plan from: the result (issues: its problems with booting, as dicts) and the machine, looked
    at once and only when a fix asks (kernels, their preset files)."""

    def __init__(self, res):
        self.res = res if isinstance(res, dict) else {}
        boot = self.res.get("boot")
        self.issues = [i for i in boot if isinstance(i, dict)] if isinstance(boot, list) else []
        self.su = engine.elevation_command()
        self._kernels = self._presets = None
        self.planned = []                       # what the fixes before this one planned

    def kinds(self):
        return {i.get("kind") for i in self.issues}

    def of(self, kind):
        return [i for i in self.issues if i.get("kind") == kind]

    def scopes(self, key):
        """The packages the fixes planned so far with this key are for."""
        return {scope_of(f["id"]) for f in self.planned if f["key"] == key} - {None}

    def kernels(self):
        """{package: kernel version} of the installed kernels (engine.kernel_packages)."""
        if self._kernels is None:
            self._kernels = {p: k for p, k in engine.kernel_packages().items()
                             if NAME.fullmatch(p) and KVER.fullmatch(k)}
        return self._kernels

    def presets(self):
        """{package: [(kind, path)]} of the installed kernels that have a mkinitcpio preset."""
        if self._presets is None:
            self._presets = {}
            for pkg in sorted(self.kernels()):
                files = [(kind, f) for kind, f in engine.preset_files(pkg) if path_ok(f)]
                if files:
                    self._presets[pkg] = files
        return self._presets

    def image_specs(self, pkgs=None):
        """The runner's specs of the boot files mkinitcpio makes for these kernels (all with a preset when None):
        i:<kernel version>:<1 when it has modules>:<image>, u:<unified kernel image>, k:<kernel file>."""
        specs = []
        for pkg, files in sorted(self.presets().items()):
            if pkgs is not None and pkg not in pkgs:
                continue
            kver = self.kernels()[pkg]
            for kind, f in files:
                spec = {"image": f"i:{kver}:{1 if engine.modular(kver) else 0}:{f}", "uki": f"u:{f}",
                        "kernel": f"k:{f}"}.get(kind)
                if spec and spec not in specs:
                    specs.append(spec)
        return specs


def plan(res):
    """The fixes for that result (an update's, or its summary read back), in the order they run: a list of fix()
    dicts, [] when the catalogue has nothing for it. A fix that cannot tell (an error looking at the machine) is
    left out; never the others. An id is there once: a second fix with the same id is left out (and said)."""
    ctx = Context(res)
    for mod in CATALOGUE:
        try:
            for f in mod.plan(ctx):
                if any(g["id"] == f["id"] for g in ctx.planned):
                    _error(f"fixes.{mod.KEY}: {f['id']} уже в плане, второй раз не беру")
                    continue
                ctx.planned.append(f)
        except Exception:
            _error(f"fixes.{mod.KEY}: {traceback.format_exc()}")
    space.finish(ctx)
    return ctx.planned


def _error(text):
    if getattr(engine, "LOG", None):
        engine.LOG.event("ОШИБКА", text)


def known(fixes):
    """The fixes this version can run, each with its id (one saved by the previous version, without an id, is its
    key's), each id once (the first)."""
    keys = {m.KEY for m in CATALOGUE}
    out, ids = [], set()
    for f in fixes or []:
        if not (isinstance(f, dict) and f.get("key") in keys and isinstance(f.get("args"), list)
                and all(isinstance(a, str) and "\0" not in a and a != "--" for a in f["args"])):
            continue
        fid = f.get("id", f["key"])
        if not isinstance(fid, str):
            continue
        key, at, scope = fid.partition("@")
        if key != f["key"] or (at and not NAME.fullmatch(scope)) or fid in ids:
            continue
        ids.add(fid)
        out.append(f if f.get("id") == fid else dict(f, id=fid))
    return out


def runner():
    """The whole root shell: the helpers and the driver, with every fix's functions."""
    return RUNNER_HEAD + "".join(m.SCRIPT for m in CATALOGUE) + RUNNER_MAIN


def command(fixes, result=None):
    """The fixes as one command for the terminal (sudo asks once there): [su, bash, -c, runner, …]. result: a file
    this user made (in a folder of theirs, not /tmp: a root shell may not write into another user's file there),
    where the runner writes one line per fix: "<id> <state> [<word>…]" (RESULT_STATES; read_results). [] when none
    of them is known."""
    fixes = known(fixes)
    if not fixes:
        return []
    argv = [engine.elevation_command(), "bash", "-c", runner(), "arch-update-fix", result or "/dev/null",
            engine.PACMAN_LOCK]
    for f in fixes:
        argv += [f["id"], *f["args"], "--"]
    return argv


def result_file():
    """A file for the runner's results, in the state folder (None when it cannot be made)."""
    try:
        os.makedirs(engine.STATE_DIR, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="fix-result-", dir=engine.STATE_DIR)
        os.close(fd)
        return path
    except OSError:
        return None


class Results(list):
    """What the runner wrote: [(id, state)], in order (dict(results) gives each fix's state), and with it
      extra {id: [word…]}  what it said beside the state: "pacman" (refused: pacman was running), "full:<mount
                           point>" (that file system ran out of room), the folder the copies stayed in
      kept                 that folder, when putting back failed (None otherwise)
      full                 the mount points that ran out of room, in order"""

    def __init__(self, pairs=(), extra=None):
        super().__init__(pairs)
        self.extra = {i: list(w) for i, w in (extra or {}).items()}

    @property
    def kept(self):
        for fid, state in self:
            if state == "rollbackfailed":
                for w in self.extra.get(fid, ()):
                    if KEPT.fullmatch(w):
                        return w
        return None

    @property
    def full(self):
        out = []
        for fid in dict(self):
            for w in self.extra.get(fid, ()):
                if w.startswith("full:") and path_ok(w[5:]) and w[5:] not in out:
                    out.append(w[5:])
        return out


def read_results(path, remove=True):
    """What the runner wrote into its result file: Results ([(id, state)], with what it said beside them). A line
    with a state this version does not know is left out."""
    pairs, extra = [], {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                words = line.split()
                if len(words) >= 2 and words[1] in RESULT_STATES:
                    pairs.append((words[0], words[1]))
                    if words[2:]:
                        extra.setdefault(words[0], []).extend(words[2:])
    except (OSError, TypeError):
        pass
    if remove and path:
        try:
            os.remove(path)
        except OSError:
            pass
    return Results(pairs, extra)


def labels(fixes):
    """{id: label} of the fixes."""
    return {f["id"]: f["label"] for f in known(fixes)}


def _namer(fixes):
    """id → label; an id that is not in the plan by its key's label, when only one fix has that key."""
    plan = known(fixes)
    by_id = {f["id"]: f["label"] for f in plan}
    keys = [f["key"] for f in plan]
    by_key = {f["key"]: f["label"] for f in plan if keys.count(f["key"]) == 1}
    return lambda fid: by_id.get(fid) or by_key.get(str(fid).partition("@")[0]) or str(fid)


def _words(results, fid):
    return getattr(results, "extra", {}).get(fid, []) if fid is not None else []


def _full_of(results, fid):
    return next((w[5:] for w in _words(results, fid) if w.startswith("full:") and path_ok(w[5:])), None)


def _as_run(status, results, fixes):
    """The results to go by: as written; with none written (no file for them, a full /home) but a status of the
    runner's own, what that status says: every fix done (0), or the first one ended so."""
    if results:
        return results
    state = BY_STATUS.get(status)
    plan = known(fixes)
    if not state or not plan:
        return Results()
    return Results([(f["id"], state) for f in plan][:None if state == "ok" else 1])


def done(results, fixes=()):
    """The labels of the fixes that were done (and checked), in the order they ran."""
    name = _namer(fixes)
    return [name(fid) for fid, state in results or [] if state in DONE]


HOOK_FAILED = "файл ядра на месте, но хук pacman закончился с ошибкой — загрузка может не пройти"


def _state_text(results, fid, state):
    """What came of one fix, for the journal: its state, with the reason or the place when the runner said one."""
    if state == "refused" and "pacman" in _words(results, fid):
        return "не начато — работал pacman, ничего не менял"
    if state == "incomplete" and "hook" in _words(results, fid):
        return HOOK_FAILED
    if state == "rollbackfailed":
        kept = getattr(results, "kept", None)
        return RESULT_STATES[state] + f" — копии в {kept or '/tmp'}"
    full = _full_of(results, fid)
    if state == "rolledback" and full:
        return f"не помогло: на {full} не хватило места — вернул всё как было"
    return RESULT_STATES.get(state, state)


def _not_begun(results, fid, state, name):
    """A fix that changed nothing (it did not begin, or pacman installed nothing), in a line."""
    what = f"«{name(fid)}» " if fid is not None else ""
    if state == "refused" and "pacman" in _words(results, fid):
        return f"{what}не начато: сейчас работает pacman — подожди, пока он закончит, и нажми F ещё раз"
    if state == "nobackup":
        return f"{what}не начато: не смог сохранить копии — ничего не менял"
    if state == "unknown":
        return f"{what}не начато: этой версии оно незнакомо"
    if state == "unchanged":
        return f"{what.rstrip()}: pacman ничего не поставил — ничего не менял" if what else \
            "pacman ничего не поставил — ничего не менял"
    return f"{what}не начато: сейчас нельзя — ничего не менял"


def outcome(status, results, fixes=()):
    """(kind, text) of a run: kind "ok" (every fix done and checked; "partial" among them: room enough for the fixes,
    not yet for the update), "notenough", "rolledback", "rollbackfailed", "incomplete" (the kernel is back, its
    initramfs may not be whole: red), "stopped" (some done, one changed nothing), "nothing" (nothing was changed: no
    copies, refused, pacman installed nothing, sudo said no) or "error"; text says it in a line, naming the fix that
    was not done (done() names the ones that were). results None: there was no file for them (a full /home), the
    exit status alone tells."""
    name = _namer(fixes)
    results = _as_run(status, results, fixes)
    states = [s for _, s in results]
    if status == OK and results and all(s in DONE for s in states):
        if "partial" in states:
            return "ok", "исправлено и проверено; для обновления места ещё мало"
        return "ok", "исправлено и проверено"

    def first(*wanted):
        return next((fid for fid, s in results if s in wanted), None)

    def quoted(fid, after=" "):
        return f"«{name(fid)}»{after}" if fid is not None else ""
    fid = first("rollbackfailed")
    if fid is not None or status == ROLLBACK_FAILED:
        kept = getattr(results, "kept", None) or "/tmp"
        return "rollbackfailed", f"{quoted(fid)}не помогло, и вернуть как было удалось не всё — копии в {kept}, " \
                                 "НЕ перезагружайся"
    fid = first("incomplete")
    if fid is not None or status == INCOMPLETE:
        if "hook" in _words(results, fid):          # another of pacman's hooks than mkinitcpio's
            return "incomplete", quoted(fid, ": ") + HOOK_FAILED
        return "incomplete", f"{quoted(fid, ': ')}файл ядра на месте, но mkinitcpio сообщил об ошибках — " \
                             "initramfs может быть неполным"
    if first("notenough") is not None or status == NOT_ENOUGH:
        return "notenough", "места на диске всё ещё мало"
    fid = first("rolledback")
    if fid is not None or status == ROLLED_BACK:
        full = _full_of(results, fid)
        if full:
            return "rolledback", f"{quoted(fid)}не помогло: на {full} не хватило места — вернул всё как было"
        return "rolledback", f"{quoted(fid)}не помогло — вернул всё как было"
    fid = first("refused", "nobackup", "unknown", "unchanged")
    if fid is not None or status in (NO_BACKUP, REFUSED, UNCHANGED):
        state = dict(results).get(fid) if fid is not None else BY_STATUS.get(status)
        what = _not_begun(results, fid, state, name)
        made = done(results, fixes)
        if made:                                # the others did their part
            return "stopped", f"сделано: {', '.join(made)}; {what}"
        return "nothing", what
    if status is None:
        return "nothing", "не запустилось"
    if status == SUDO_REFUSED:
        return "nothing", "пароль sudo не принят — ничего не менял"
    if not results:
        return "nothing", f"не начато (код {status}: пароль не принят или отменён?)"
    return "error", f"закончилось с ошибкой (код {status})"


def journal_text(status, results, fixes=()):
    """One line for the update's journal: every fix and what came of it (with the reason, the place or the folder
    of the copies when the runner said one); with no results, every planned fix and why none began."""
    name = _namer(fixes)
    results = _as_run(status, results, fixes)
    if results:
        return "; ".join(f"{name(fid)}: {_state_text(results, fid, s)}" for fid, s in results) + f" (код {status})"
    planned = ", ".join(name(f["id"]) for f in known(fixes))
    if not planned:
        return f"ничего не сделано (код {status})"
    if status is None:
        return f"{planned}: не запустилось"
    if status == SUDO_REFUSED:
        return f"{planned}: не начато — пароль sudo не принят (код {status})"
    return f"{planned}: не начато (код {status}: пароль не принят или отменён?)"


# ───────────────────────────── the root shell ─────────────────────────────
# Arguments: <result file> <pacman's lock> then each fix as <id> <args…> --. Runs every fix in turn; one that was
# not done holds up the fixes after it (all of them, or for a fix of one kernel those of that kernel: see the
# module's docstring), and the run exits with the status of the first one not done (see OK…UNCHANGED).
RUNNER_HEAD = r"""
set -u
umask 077
export LANGUAGE=en LC_MESSAGES=C.UTF-8 LC_NUMERIC=C.UTF-8
result=${1:-/dev/null} lock=${2:-/var/lib/pacman/db.lck}
shift 2
# What came of each fix goes into the user's own result file: only a plain file of theirs (never through a link
# somebody put there), opened once.
if [ "$result" != /dev/null ]; then
    if [ -L "$result" ] || [ ! -f "$result" ] \
            || { [ -n "${SUDO_UID:-}" ] && [ "$(stat -c %u -- "$result" 2>/dev/null)" != "$SUDO_UID" ]; }; then
        result=/dev/null
    fi
fi
exec 3>>"$result" || exec 3>/dev/null
keep_work=0
work=$(mktemp -d /tmp/arch-update-fix.XXXXXXXX 2>/dev/null) || work=
trap 'if [ -n "$work" ] && [ "$keep_work" != 1 ]; then rm -rf -- "$work"; fi' EXIT
# never cut off halfway: a half-written initramfs is worse than none (pacman guards itself the same way)
trap '' INT QUIT TSTP HUP
# what mkinitcpio says when it built no image, or one that may not boot (in its own output, or in pacman's log when
# pacman's hook ran it)
INITCPIO_TROUBLE='errors were encountered during the build|No modules were added to the image|image generation FAILED|No space left on device'
# what a person puts the copies back with, by hand
case ${SUDO_USER:+s}${DOAS_USER:+d} in d) as_root=doas ;; *) as_root=sudo ;; esac

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
good() { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
bad() { printf '\033[31m  ✗ %s\033[0m\n' "$*"; }
note() { printf '%s\n' "$*" >&3 2>/dev/null || true; }
free_bytes() { df --output=avail -B1 -- "$1" 2>/dev/null | tail -n 1 | tr -dc 0-9; }
size() {
    awk -v b="${1:-0}" 'BEGIN { if (b < 0) b = -b
        if (b >= 1073741824) printf "%.1f ГБ", b / 1073741824
        else if (b >= 1048576) printf "%d МБ", b / 1048576
        else printf "%d КБ", b / 1024 }' | tr . ,
}
nonempty() {
    if [ -s "$1" ]; then good "$1 на месте"; else bad "$1 нет или он пустой"; return 1; fi
}
# drain: keys pressed before a question are no answer to it
drain() {
    local _
    while read -r -s -t 0.2 -n 1 _ 2>/dev/null; do :; done
}

# room_for <file>…: room in /tmp for copies of them, with some to spare
room_for() {
    local need=0 f s avail
    for f; do
        [ -e "$f" ] || continue
        s=$(stat -c %s -- "$f" 2>/dev/null) || return 1
        need=$((need + s))
    done
    avail=$(free_bytes "$work")
    if [ -z "$avail" ] || [ $((need + need / 10 + 16777216)) -gt "$avail" ]; then
        bad "в /tmp мало места для копий: нужно $(size "$need"), свободно $(size "${avail:-0}")"
        return 1
    fi
}

# keep <file>…: a copy of each in $work ($work/<n>, its sum <n>.sum and where it belongs <n>.path), to put back if
# the fix does not work. A file that is not there is remembered as missing (<n>.missing): putting back removes what
# the fix made in its place.
kept=()
keep() {
    local f i
    for f; do
        i=${#kept[@]}
        kept+=("$f")
        printf '%s\n' "$f" >"$work/$i.path" || return 1
        if [ -e "$f" ]; then
            cp --preserve=mode,timestamps -- "$f" "$work/$i" || return 1
            sha256sum <"$work/$i" >"$work/$i.sum" || return 1
        else
            : >"$work/$i.missing" || return 1
        fi
    done
}

# keep_missing <file>…: remembered as missing whatever is there now (no copy): putting back removes it. For
# mkinitcpio's "<image>.tmp" (tmps_of): one that is there is what a build that failed left, never a file to bring back,
# and nothing booting needs: one that cannot be removed (a read-only ESP) is said, not a rollback that failed.
keep_missing() {
    local f i
    for f; do
        if [ -d "$f" ] && [ ! -L "$f" ]; then bad "$f — папка, а не файл"; return 1; fi
        i=${#kept[@]}
        kept+=("$f")
        printf '%s\n' "$f" >"$work/$i.path" || return 1
        : >"$work/$i.missing" || return 1
        : >"$work/$i.leftover" || return 1
    done
}

# kept_same: every kept file still as it was (a missing one still missing): nothing to put back
kept_same() {
    local i f
    for i in "${!kept[@]}"; do
        f=${kept[$i]}
        if [ -e "$work/$i.missing" ]; then
            if [ -e "$f" ] || [ -L "$f" ]; then return 1; fi
        elif [ ! -e "$f" ] || [ "$(sha256sum <"$f")" != "$(cat "$work/$i.sum")" ]; then
            return 1
        fi
    done
}

# copy_back <copy> <file>: the copy written as that file, with the copy's mode and times where its file system
# keeps them (a FAT /boot has no modes: that is no failure)
copy_back() {
    cp -- "$1" "$2" 2>/dev/null || return 1
    chmod --reference="$1" -- "$2" 2>/dev/null
    touch --reference="$1" -- "$2" 2>/dev/null
    return 0
}

# put_back: every kept file as it was, each checked against its copy. First what the fix made where there was
# nothing (mkinitcpio's "<image>.tmp" among them: the room it holds may be what the copies need), then the others,
# each replaced in one step (written beside it, then moved over it). With no room beside it, the file the fix left
# goes first, but only when the copy then fits in what is free with that file gone: the copy's size in whole blocks
# of that file system and one block more (ext4 may need one for the file's extents when the room is in pieces; a
# full /boot is the usual case here, and a file a writer cut short there holds all the room the copy had before).
# Otherwise it stays, and the commands that put the copies back by hand are printed.
put_back() {
    local i f d s freed avail m failed=() left bs need
    for i in "${!kept[@]}"; do
        [ -e "$work/$i.missing" ] || continue
        f=${kept[$i]}
        if { [ -e "$f" ] || [ -L "$f" ]; } && ! rm -f -- "$f"; then
            if [ -e "$work/$i.leftover" ]; then warn "не смог убрать $f — для загрузки он не нужен, но занимает место"
            else failed+=("$i"); fi
        fi
    done
    for i in "${!kept[@]}"; do
        [ -e "$work/$i.missing" ] && continue
        f=${kept[$i]}
        if [ -e "$f" ] && [ "$(sha256sum <"$f")" = "$(cat "$work/$i.sum")" ]; then
            continue                            # untouched
        fi
        rm -f -- "$f.arch-update-new"
        if copy_back "$work/$i" "$f.arch-update-new"; then
            if ! mv -f -- "$f.arch-update-new" "$f"; then
                rm -f -- "$f.arch-update-new"
                failed+=("$i")
                continue
            fi
        else
            rm -f -- "$f.arch-update-new"       # what did fit of it
            d=$(dirname -- "$f")
            s=$(stat -c %s -- "$work/$i")
            freed=0
            if [ -f "$f" ] && [ "$(stat -c %h -- "$f")" = 1 ]; then
                freed=$(stat -c '%b %B' -- "$f" | awk '{ print $1 * $2 }')
            fi
            avail=$(free_bytes "$d")
            left=$((${avail:-0} + freed))
            bs=$(stat -f -c %S -- "$d" 2>/dev/null) || bs=
            case $bs in ''|*[!0-9]*|0) bs=4096 ;; esac
            need=$(( (s + bs - 1) / bs * bs + bs ))
            if [ -z "$avail" ] || [ "$left" -lt "$need" ]; then
                m=$(stat -c %m -- "$d" 2>/dev/null) || m=$d
                bad "на $m нет места, чтобы вернуть $f: нужно $(size "$need"), а будет свободно $(size "$left")" \
                    "— не хватает $(size $((need - left)))"
                failed+=("$i")
                continue
            fi
            rm -f -- "$f"
            if ! copy_back "$work/$i" "$f"; then
                rm -f -- "$f"                   # half a copy is worse than none (the copy stays in $work)
                failed+=("$i")
                continue
            fi
        fi
        if [ "$(sha256sum <"$f")" != "$(cat "$work/$i.sum")" ]; then failed+=("$i"); fi
    done
    sync
    if [ ${#failed[@]} -gt 0 ]; then
        keep_work=1
        bad "не смог вернуть как было:$(for i in "${failed[@]}"; do printf ' %s' "${kept[$i]}"; done)"
        bad "копии остались в $work — НЕ перезагружайся, пока они не на месте"
        printf '  Вернуть их на место можно так (в терминале):\n'
        for i in "${failed[@]}"; do
            if [ -e "$work/$i.missing" ]; then
                printf '    %s rm -f -- %q\n' "$as_root" "${kept[$i]}"
            else
                printf '    %s cp -- %q %q\n' "$as_root" "$work/$i" "${kept[$i]}"
            fi
        done
        return 1
    fi
    if [ ${#kept[@]} -gt 0 ]; then good "все файлы как были (сверил с копиями)"; fi
}

clear_kept() {
    if [ -n "$work" ] && [ "$keep_work" != 1 ]; then rm -rf -- "${work:?}"/*; fi
    kept=()
}

# tmps_of <specs…>: the "<image>.tmp" of every image (i:) and UKI (u:) there, one a line. mkinitcpio writes the new
# image into it first when it guesses there is room for 1.25 times the old one, renames it when the build went well,
# and leaves it behind (often all the room there was) when the build failed: one that is there is never anything
# but such a leftover.
tmps_of() {
    local a
    for a; do
        case $a in
            i:*) printf '%s.tmp\n' "${a#i:*:*:}" ;;
            u:*) printf '%s.tmp\n' "${a#u:}" ;;
        esac
    done
}
# drop_tmps <specs…>: those leftovers removed, each said (kept with keep_missing first): their room is what the next
# build needs. Status 1 when one could not be removed.
drop_tmps() {
    local f rc=0
    while IFS= read -r f; do
        if [ -e "$f" ] || [ -L "$f" ]; then
            step "Убираю $f: его оставил mkinitcpio, который не дописал образ"
            rm -f -- "$f" || rc=1
        fi
    done < <(tmps_of "$@")
    return "$rc"
}

# ran_full <for what> <output> <specs…>: after a step that failed, whether a file system ran out of room for it, and
# which: the one a path a "No space left on device" line of that output names is on (the nearest folder of it that
# is there: what failed may be gone); else, of the boot files' folders in the specs (i:, u:, k:), then the folder of
# the package's own kernel (v:, where pacman writes) and the build folder (in /tmp), one with under 1 MiB free, or,
# when the output said so with no path (zstd), the one with the least room. Looked at right away, before a rollback or
# anything else gives the room back. Said (the free figure only when it was measured under 1 MiB: objcopy and
# mkinitcpio give their room back as they stop, and "ran out, 39 MB free" would contradict itself), and noted with
# the fix (full:<mount point>).
ran_full() {
    local what=$1 out=$2 a d p m avail low='' lowest='' least='' said='' full dirs=()
    shift 2
    full=$(grep -i 'No space left on device' -- "$out" 2>/dev/null)
    while IFS= read -r p; do
        while [ -n "$p" ] && [ ! -e "$p" ]; do p=${p%/*}; done
        low=${p:-/}
        break
    done < <(grep -oE "(^|[[:space:]'\"(=])/[^[:space:]'\"(),:[]+" <<<"$full" | sed 's|^[^/]*||')
    if [ -z "$low" ]; then
        for a; do
            case $a in
                i:*) dirs+=("$(dirname -- "${a#i:*:*:}")") ;;
                u:*|k:*) dirs+=("$(dirname -- "${a#?:}")") ;;
            esac
        done
        for a; do case $a in v:/*) dirs+=("$(dirname -- "${a#v:}")") ;; esac; done
        dirs+=("${TMPDIR:-/tmp}")
        for d in "${dirs[@]}"; do
            avail=$(free_bytes "$d")
            [ -n "$avail" ] || continue
            if [ "$avail" -lt 1048576 ]; then low=$d; break; fi
            if [ -z "$least" ] || [ "$avail" -lt "$least" ]; then least=$avail lowest=$d; fi
        done
        if [ -z "$low" ] && [ -n "$full" ]; then low=$lowest; fi
    fi
    [ -n "$low" ] || return 0
    m=$(stat -c %m -- "$low" 2>/dev/null) || m=$low
    avail=$(free_bytes "$low")
    if [ -n "$avail" ] && [ "$avail" -lt 1048576 ]; then said=": свободно $(size "$avail")"; fi
    bad "на $m не хватило места${what:+ $what}$said"
    case $m in /*[[:space:]]*|'') ;; /*) note_extra="${note_extra:+$note_extra }full:$m" ;; esac
}

# image_trouble i:<kernel version>:<1 if it has modules>:<image>: what is wrong with that image, in a line (status
# 1); nothing when it is there, built for that kernel and with its modules (mkinitcpio makes the kernel's folder in
# every image, and its module list only when it put modules in)
image_trouble() {
    local spec=${1#i:} kver m f list
    kver=${spec%%:*}
    spec=${spec#*:}
    m=${spec%%:*}
    f=${spec#*:}
    if [ ! -s "$f" ]; then printf '%s нет или он пустой\n' "$f"; return 1; fi
    if ! list=$(timeout 120 lsinitcpio "$f" 2>/dev/null); then printf '%s не читается (lsinitcpio)\n' "$f"; return 1; fi
    if ! grep -qF -- "usr/lib/modules/$kver/" <<<"$list"; then
        printf '%s собран не под ядро %s\n' "$f" "$kver"
        return 1
    fi
    if [ "$m" = 1 ] && ! grep -qxF -e "usr/lib/modules/$kver/modules.dep.bin" \
            -e "usr/lib/modules/$kver/modules.dep" <<<"$list"; then
        printf 'в %s нет модулей ядра %s\n' "$f" "$kver"
        return 1
    fi
}
# image_ok <image spec>: the same, said
image_ok() {
    local why spec=${1#i:}
    if ! why=$(image_trouble "$1"); then bad "$why"; return 1; fi
    good "${spec#*:*:} собран под ядро ${spec%%:*}, модули в нём"
}
"""

RUNNER_MAIN = r"""
run_fix() {
    local id=$1 key=${1%%@*}
    shift
    kept=()
    failed_as='' done_as='' note_extra=''
    if ! declare -F "${key}_apply" >/dev/null; then
        bad "исправления «$key» в этой версии нет"
        note "$id unknown"
        return 82
    fi
    if [ -e "$lock" ] && pgrep -x pacman >/dev/null 2>&1; then
        bad "сейчас работает pacman — подожди, пока он закончит, и нажми F ещё раз"
        note "$id refused pacman"
        return 83
    fi
    if declare -F "${key}_check" >/dev/null && ! "${key}_check" "$@"; then
        note "$id refused${note_extra:+ $note_extra}"
        return 83
    fi
    if [ -z "$work" ] || ! "${key}_backup" "$@"; then
        bad "не смог сохранить копии — ничего не менял"
        note "$id nobackup"
        clear_kept
        return 82
    fi
    # what the fix writes gets the system's usual modes (module lists and DKMS modules readable by all, as pacman
    # writes them); the copies, sums and notes in $work stay root's only
    umask 022
    if "${key}_apply" "$@" && "${key}_verify" "$@"; then
        umask 077
        good "готово, проверено"
        note "$id ${done_as:-ok}${note_extra:+ $note_extra}"
        clear_kept
        return 0
    fi
    umask 077
    case $failed_as in
        notenough|incomplete|unchanged)        # nothing to undo
            note "$id $failed_as${note_extra:+ $note_extra}"
            clear_kept
            case $failed_as in notenough) return 84 ;; incomplete) return 85 ;; *) return 86 ;; esac
            ;;
    esac
    step "Не помогло — возвращаю всё как было"
    if undo "$key" "$@"; then
        note "$id rolledback${note_extra:+ $note_extra}"
        clear_kept
        return 80
    fi
    note "$id rollbackfailed $work${note_extra:+ $note_extra}"
    return 81
}

# undo <key> <args…>: the fix's own way back (<key>_rollback, when it has one), then every kept file put back
undo() {
    local key=$1 rc=0
    shift
    if declare -F "${key}_rollback" >/dev/null; then
        umask 022
        "${key}_rollback" "$@" || rc=1
        umask 077
    fi
    put_back || rc=1
    return "$rc"
}

# held <scope>: a fix for that kernel was not done before
held=" "
held() { case $held in *" $1 "*) return 0 ;; esac; return 1; }

status=0 stop=0
while [ $# -gt 0 ]; do
    id=$1
    shift
    args=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do
        args+=("$1")
        shift
    done
    [ $# -gt 0 ] && shift
    scope=
    case $id in *@*) scope=${id#*@} ;; esac
    if [ "$stop" = 1 ] || { [ -n "$scope" ] && held "$scope"; }; then
        note "$id skipped"
        continue
    fi
    run_fix "$id" "${args[@]}"
    rc=$?
    [ "$rc" -eq 0 ] && continue
    [ "$status" -ne 0 ] || status=$rc
    # one for a kernel holds up only what comes after it for that kernel; one for the whole machine everything
    # after it, and so does one whose copies could not all be put back (they stay in $work, where nothing may
    # write over them, and the machine is not as it was)
    if [ -n "$scope" ] && [ "$rc" -ne 81 ]; then held="$held$scope "; else stop=1; fi
done
exit "$status"
"""

from fixes import space, mount, depmod, kernel, dkms, initramfs, sbctl  # noqa: E402  (they use the above)

CATALOGUE = [space, mount, depmod, kernel, dkms, initramfs, sbctl]
