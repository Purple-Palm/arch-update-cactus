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
           in /tmp and the fix says where.
After the root shell, the engine looks at the boot files again (engine.recheck_boot): the update's warning goes
only when that look finds nothing wrong.

A fix module has:
  KEY      its name: in the runner (the bash functions <KEY>_backup, <KEY>_apply, <KEY>_verify and, when putting
           the kept files back is not the way to undo it, <KEY>_rollback), in the result file and in the journal
  SCRIPT   those bash functions; they may use the helpers of RUNNER (step, good, warn, bad, keep, room_for,
           put_back, image_ok, nonempty, free_bytes, size) and set failed_as to end without a rollback (nothing
           to undo, e.g. "notenough")
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
    "rolledback": "не помогло — вернул всё как было",
    "rollbackfailed": "не помогло, и вернуть как было удалось не всё",
    "nobackup": "не смог сохранить копии — ничего не менял",
    "refused": "не начал: сейчас нельзя",
    "notenough": "сделал, что мог, но этого мало",
    "skipped": "не делал (не прошло исправление перед ним)",
    "unknown": "этой версии оно незнакомо",
}
# The runner's exit status: 0 every fix done and checked. The first one that was not ends the run with
OK, ROLLED_BACK, ROLLBACK_FAILED, NO_BACKUP, REFUSED, NOT_ENOUGH = 0, 80, 81, 82, 83, 84
NAME = re.compile(r"[\w.+@-]+")          # a package or a DKMS module
KVER = re.compile(r"[\w.+~-]+")          # a kernel version
BOOT_TARGETS = ("/boot", "/efi", "/boot/efi")
GIB = 2 ** 30


def path_ok(p):
    return isinstance(p, str) and p.startswith("/") and "\n" not in p and "\0" not in p and ".." not in p.split("/")


def fix(key, label, argv, args, how=None):
    """One planned fix: key, label (for F and the journal), argv (the same by hand, one command: what
    arch-update-auto prints), how (the same by hand as a shell line), args (for the runner)."""
    return {"key": key, "label": label, "argv": list(argv), "how": how or " ".join(_quote(a) for a in argv),
            "args": [str(a) for a in args]}


def _quote(a):
    return a if re.fullmatch(r"[\w./:=@+-]+", a) else "'" + a.replace("'", "'\\''") + "'"


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
    left out; never the others."""
    ctx = Context(res)
    for mod in CATALOGUE:
        try:
            ctx.planned += mod.plan(ctx)
        except Exception:
            if getattr(engine, "LOG", None):
                engine.LOG.event("ОШИБКА", f"fixes.{mod.KEY}: {traceback.format_exc()}")
    return ctx.planned


def known(fixes):
    keys = {m.KEY for m in CATALOGUE}
    return [f for f in fixes or [] if isinstance(f, dict) and f.get("key") in keys and isinstance(f.get("args"), list)
            and all(isinstance(a, str) and "\0" not in a for a in f["args"])]


def runner():
    """The whole root shell: the helpers and the driver, with every fix's functions."""
    return RUNNER_HEAD + "".join(m.SCRIPT for m in CATALOGUE) + RUNNER_MAIN


def command(fixes, result=None):
    """The fixes as one command for the terminal (sudo asks once there): [su, bash, -c, runner, …]. result: a file
    this user made (in a folder of theirs, not /tmp: a root shell may not write into another user's file there),
    where the runner writes one line per fix: "<key> <state>" (RESULT_STATES). [] when none of them is known."""
    fixes = known(fixes)
    if not fixes:
        return []
    argv = [engine.elevation_command(), "bash", "-c", runner(), "arch-update-fix", result or "/dev/null"]
    for f in fixes:
        argv += [f["key"], *f["args"], "--"]
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


def read_results(path, remove=True):
    """What the runner wrote: [(key, state)], in order."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, state = line.strip().partition(" ")
                if key and state in RESULT_STATES:
                    out.append((key, state))
    except (OSError, TypeError):
        pass
    if remove and path:
        try:
            os.remove(path)
        except OSError:
            pass
    return out


def labels(fixes):
    return {f["key"]: f["label"] for f in known(fixes)}


def outcome(status, results, fixes=()):
    """(kind, text) of a run: kind "ok", "notenough", "rolledback", "rollbackfailed", "nothing" (nothing was
    changed: no copies, refused, sudo said no) or "error"; text says it in a line."""
    names = labels(fixes)
    states = dict(results)
    if status == OK and results and all(s == "ok" for _, s in results):
        return "ok", "исправлено и проверено"
    first = next(((k, s) for k, s in results if s not in ("ok", "skipped")), None)
    name = names.get(first[0], first[0]) if first else ""
    if status == ROLLBACK_FAILED or "rollbackfailed" in states.values():
        return "rollbackfailed", f"«{name}» не помогло, и вернуть как было удалось не всё — копии в /tmp, " \
                                 "НЕ перезагружайся"
    if status == NOT_ENOUGH or "notenough" in states.values():
        return "notenough", "место освободил, но его всё ещё мало"
    if status == ROLLED_BACK or "rolledback" in states.values():
        return "rolledback", f"«{name}» не помогло — вернул всё как было"
    if status in (NO_BACKUP, REFUSED) or (first and first[1] in ("nobackup", "refused", "unknown")):
        return "nothing", f"«{name}»: {RESULT_STATES[first[1]]}" if first else "ничего не менял"
    if status is None:
        return "nothing", "не запустилось"
    if not results:
        return "nothing", f"не начал (код {status}: пароль не принят или отменён?)"
    return "error", f"закончилось с ошибкой (код {status})"


def journal_text(status, results, fixes=()):
    """One line for the update's journal: every fix and what came of it."""
    names = labels(fixes)
    parts = [f"{names.get(k, k)}: {RESULT_STATES.get(s, s)}" for k, s in results]
    return "; ".join(parts or ["ничего не сделано"]) + f" (код {status})"


# ───────────────────────────── the root shell ─────────────────────────────
# Arguments: <result file> then each fix as <key> <args…> --. Runs every fix in turn, stops at the first that was
# not done (the next ones build on it), and exits with its status (see OK…NOT_ENOUGH).
RUNNER_HEAD = r"""
set -u
umask 077
export LANGUAGE=en LC_MESSAGES=C.UTF-8 LC_NUMERIC=C.UTF-8
result=${1:-/dev/null}
shift
keep_work=0
work=$(mktemp -d /tmp/arch-update-fix.XXXXXXXX 2>/dev/null) || work=
trap 'if [ -n "$work" ] && [ "$keep_work" != 1 ]; then rm -rf -- "$work"; fi' EXIT
# never cut off halfway: a half-written initramfs is worse than none (pacman guards itself the same way)
trap '' INT QUIT TSTP HUP

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
good() { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
bad() { printf '\033[31m  ✗ %s\033[0m\n' "$*"; }
note() { printf '%s\n' "$*" >>"$result" 2>/dev/null || true; }
free_bytes() { df --output=avail -B1 -- "$1" 2>/dev/null | tail -n 1 | tr -dc 0-9; }
size() {
    awk -v b="${1:-0}" 'BEGIN { if (b < 0) b = -b
        if (b >= 1073741824) printf "%.1f ГБ", b / 1073741824; else printf "%d МБ", b / 1048576 }' | tr . ,
}
nonempty() {
    if [ -s "$1" ]; then good "$1 на месте"; else bad "$1 нет или он пустой"; return 1; fi
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

# keep <file>…: a copy of each in $work, to put back if the fix does not work. A file that is not there is
# remembered as missing: putting back removes what the fix made in its place.
kept=()
keep() {
    local f i
    for f; do
        i=${#kept[@]}
        kept+=("$f")
        if [ -e "$f" ]; then
            cp --preserve=mode,timestamps -- "$f" "$work/$i" || return 1
            sha256sum <"$work/$i" >"$work/$i.sum" || return 1
        else
            : >"$work/$i.missing" || return 1
        fi
    done
}

# put_back: every kept file as it was, each checked against its copy. A file is replaced in one step (written
# beside it, then moved over it); with no room beside it, the one the fix left goes first.
put_back() {
    local i f failed=()
    for i in "${!kept[@]}"; do
        f=${kept[$i]}
        if [ -e "$work/$i.missing" ]; then
            if [ -e "$f" ] && ! rm -f -- "$f"; then failed+=("$f"); fi
            continue
        fi
        if [ -e "$f" ] && [ "$(sha256sum <"$f")" = "$(cat "$work/$i.sum")" ]; then
            continue                            # untouched
        fi
        if cp -- "$work/$i" "$f.arch-update-new" 2>/dev/null; then
            chmod --reference="$work/$i" -- "$f.arch-update-new" 2>/dev/null
            touch --reference="$work/$i" -- "$f.arch-update-new" 2>/dev/null
            if ! mv -f -- "$f.arch-update-new" "$f"; then
                rm -f -- "$f.arch-update-new"
                failed+=("$f")
                continue
            fi
        else
            rm -f -- "$f.arch-update-new" "$f"
            if ! cp -- "$work/$i" "$f" 2>/dev/null; then
                failed+=("$f")
                continue
            fi
        fi
        if [ "$(sha256sum <"$f")" != "$(cat "$work/$i.sum")" ]; then failed+=("$f"); fi
    done
    sync
    if [ ${#failed[@]} -gt 0 ]; then
        bad "не смог вернуть как было: ${failed[*]}"
        keep_work=1
        bad "копии остались в $work — НЕ перезагружайся, пока они не на месте"
        return 1
    fi
    if [ ${#kept[@]} -gt 0 ]; then good "все файлы как были (сверил с копиями)"; fi
}

clear_kept() {
    if [ -n "$work" ] && [ "$keep_work" != 1 ]; then rm -rf -- "${work:?}"/*; fi
    kept=()
}

# image_ok i:<kernel version>:<1 if it has modules>:<image>: there, built for that kernel, with its modules
# (mkinitcpio makes the kernel's folder in every image, and its module list only when it put modules in)
image_ok() {
    local spec=${1#i:} kver m f list
    kver=${spec%%:*}
    spec=${spec#*:}
    m=${spec%%:*}
    f=${spec#*:}
    if [ ! -s "$f" ]; then bad "$f нет или он пустой"; return 1; fi
    if ! list=$(timeout 120 lsinitcpio "$f" 2>/dev/null); then bad "$f не читается (lsinitcpio)"; return 1; fi
    if ! grep -qF -- "usr/lib/modules/$kver/" <<<"$list"; then bad "$f собран не под ядро $kver"; return 1; fi
    if [ "$m" = 1 ] && ! grep -qxF -e "usr/lib/modules/$kver/modules.dep.bin" \
            -e "usr/lib/modules/$kver/modules.dep" <<<"$list"; then
        bad "в $f нет модулей ядра $kver"
        return 1
    fi
    good "$f собран под ядро $kver, модули в нём"
}
"""

RUNNER_MAIN = r"""
run_fix() {
    local key=$1
    shift
    kept=()
    failed_as=
    if ! declare -F "${key}_apply" >/dev/null; then
        bad "исправления «$key» в этой версии нет"
        note "$key unknown"
        return 82
    fi
    if pgrep -x pacman >/dev/null 2>&1; then
        bad "сейчас работает pacman — дождись, пока он закончит, и попробуй снова"
        note "$key refused"
        return 83
    fi
    if declare -F "${key}_check" >/dev/null && ! "${key}_check" "$@"; then
        note "$key refused"
        return 83
    fi
    if [ -z "$work" ] || ! "${key}_backup" "$@"; then
        bad "не смог сохранить копии — ничего не менял"
        note "$key nobackup"
        clear_kept
        return 82
    fi
    if "${key}_apply" "$@" && "${key}_verify" "$@"; then
        good "готово, проверено"
        note "$key ok"
        clear_kept
        return 0
    fi
    if [ -n "$failed_as" ]; then
        note "$key $failed_as"
        clear_kept
        return 84
    fi
    step "Не помогло — возвращаю всё как было"
    if undo "$key" "$@"; then
        note "$key rolledback"
        clear_kept
        return 80
    fi
    note "$key rollbackfailed"
    return 81
}

# undo <key> <args…>: the fix's own way back (<key>_rollback, when it has one), then every kept file put back
undo() {
    local key=$1
    shift
    if declare -F "${key}_rollback" >/dev/null && ! "${key}_rollback" "$@"; then
        put_back
        return 1
    fi
    put_back
}

status=0
while [ $# -gt 0 ]; do
    key=$1
    shift
    args=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do
        args+=("$1")
        shift
    done
    [ $# -gt 0 ] && shift
    if [ "$status" -ne 0 ]; then
        note "$key skipped"
        continue
    fi
    run_fix "$key" "${args[@]}"
    status=$?
done
exit "$status"
"""

from fixes import space, mount, depmod, kernel, dkms, initramfs, sbctl  # noqa: E402  (they use the above)

CATALOGUE = [space, mount, depmod, kernel, dkms, initramfs, sbctl]
