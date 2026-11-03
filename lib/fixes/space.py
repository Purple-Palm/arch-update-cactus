"""«Освободить место»: room on the root file system without touching anything the system needs.

When: the update stopped before it began because the disk was too small for it (res["space"]: the engine's
check before pacman's transaction, which counts what a snapper snapshot keeps), the disk ran full during the
update (ENOSPC in the output: engine.DISK_FULL), or other fixes are about to run on a nearly full disk (depmod
and mkinitcpio need room to write). Only while / really has less free than wanted: a full disk elsewhere (/tmp of
an AUR build, a /boot partition) is not freed by this, and nothing is deleted for nothing.

Two amounts: what is wanted (the update's need, or 2 GiB after a disk that ran full) and the least the fixes after
this one need to work: LOW (512 MiB) when one of them writes much to /: a kernel reinstall (pacman -U writes the
package there, and a snapshot keeps the files it replaces), a DKMS build (under /var/lib/dkms), an initramfs rebuilt
onto the root file system; else FEW (64 MiB): the module lists depmod writes, an initramfs on a /boot of its own
(mkinitcpio builds in /tmp, and the fix checks the room for its copies itself), a mount or a signature need next to
nothing on /. Ending above the least but below the wanted is "partial": the fixes after it still run (an
unbootable machine is mended first), and the owner is told the update needs more room.

What, in order, each step only while there is still too little room (and none at all when there is enough from
the start):
  1. pacman's cache down to the versions installed now (pacman -Sc): what a reinstall would need stays;
  2. the systemd journal down to its newest 200 MB;
  3. snapper's snapshots, config by config (root, home…: on the same disk as / only), only those the config itself
     lets snapper clean up (NUMBER_CLEANUP=yes: by number, as snap-pac's are; TIMELINE_CLEANUP=yes: by age, as the
     hourly ones of archinstall's snapper setup are), and only when the user says yes in the terminal (no by
     default, no after 5 minutes, keys pressed before the question do not count): first by the config's own limits
     (snapper cleanup number / timeline), then, if there is still too little room and the user says yes again,
     with its number limits lowered to the last 10 snapshots (and 5 important ones), which it keeps to from then
     on. A limit is only ever lowered (one of 5 stays 5, a range "2-50" becomes "2-10"), and the question says the
     values it will set. Deleted snapshots cannot be rolled back to.
btrfs gives back what deleted files and snapshots held only when it commits (by itself every 30 s): every look at
the free space here commits first, so what was freed counts at once (no needless question, no "too little" that is
not so).
Nothing to put back: what goes is caches, old journal entries and, if the user agreed, old snapshots. Verified by
the free space it leaves; when that is too little, it lists what else takes room on this machine.
"""

import os
import shutil

import engine
from fixes import GIB, fix

KEY = "space"
LOW = 512 * 2 ** 20          # below this free on / the other fixes get this one first; the least the heavy ones need
FEW = 64 * 2 ** 20           # the least the others need (depmod's lists, a /boot of its own, mount, sbctl)
AFTER_FULL = 2 * GIB         # wanted free after the disk ran full during an update
HEAVY = ("kernel", "dkms")   # fixes that write much to / whatever the machine (pacman -U, a DKMS build)


def plan(ctx):
    want = None
    space = ctx.res.get("space") if isinstance(ctx.res.get("space"), dict) else None
    now = free()
    if space and isinstance(space.get("need"), int) and space["need"] > 0:
        want = space["need"]
    elif engine.DISK_FULL in [p for p in ctx.res.get("problems") or [] if isinstance(p, str)]:
        want = AFTER_FULL
    elif ctx.issues and now is not None and now < LOW:
        want = 1 * GIB
    if want is None or (now is not None and now >= want):
        return []                               # / has the room already: whatever ran full, it is not this
    su = ctx.su
    how = f"{su} pacman -Sc; {su} journalctl --vacuum-size=200M"
    if shutil.which("snapper"):
        how += f"; {su} snapper -c root cleanup number"
    return [fix(KEY, "освободить место на диске", [su, "pacman", "-Sc"], [str(want)], how=how)]


def finish(ctx):
    """After all fixes are planned: the least this one has to leave is what the fixes after it need (least()), or
    the whole amount wanted when it is the only one (room for the update)."""
    for f in ctx.planned:
        if f["key"] == KEY and len(f["args"]) == 1:
            want = int(f["args"][0])
            others = [g for g in ctx.planned if g["key"] != KEY]
            f["args"].append(str(min(want, least(others)) if others else want))


def least(fixes):
    """What the fixes after this one need free on / at the least: LOW when one of them writes much there (HEAVY, or
    an initramfs fix whose image or UKI is on the root file system, and not under a partition a mount fix before it
    puts there), else FEW. A least that is too high leaves an unbootable machine unmended for want of room it would
    not use; one too low is caught all the same: each fix checks the room for its copies and puts everything back
    when it runs out."""
    mounted = [t for f in fixes if f["key"] == "mount" for t in f["args"]]
    for f in fixes:
        if f["key"] in HEAVY:
            return LOW
        if f["key"] == "initramfs" and any(on_root(p) and not any(p.startswith(t.rstrip("/") + "/") for t in mounted)
                                           for p in boot_files(f["args"])):
            return LOW
    return FEW


def boot_files(args):
    """The images and UKIs an initramfs fix's arguments name (i:<kver>:<0|1>:<image>, u:<UKI>)."""
    out = []
    for a in args:
        if a.startswith("i:") and a.count(":") >= 3:
            out.append(a.split(":", 3)[3])
        elif a.startswith("u:"):
            out.append(a[2:])
    return out


def on_root(path):
    """Whether that file (or, while it is not there, or this user may not look into its folder, the nearest folder
    of it that can be looked at) is on the root file system; True when there is no telling (LOW then, as before)."""
    try:
        root = os.stat("/").st_dev
    except OSError:
        return True
    p = path if isinstance(path, str) and path.startswith("/") else "/"
    while True:
        try:
            return os.stat(p).st_dev == root
        except OSError:
            if p == "/":
                return True
            p = os.path.dirname(p)


def free(path="/"):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


SCRIPT = r"""
# space <bytes wanted free on /> <the least the fixes after this one need>
space_backup() { return 0; }
# space_free: the free bytes on / now. btrfs gives back what deleted files and snapshots held only when it commits
# (by itself every 30 s): a commit first, then the look.
space_free() {
    if [ "$(stat -f -c %T / 2>/dev/null)" = btrfs ]; then
        btrfs filesystem sync / >/dev/null 2>&1 || sync -f / >/dev/null 2>&1 || true
    fi
    free_bytes /
}
# btrfs removes deleted snapshots a little later, in the background: wait for that (at most 10 minutes), then commit
space_settle() {
    if [ "$(stat -f -c %T / 2>/dev/null)" = btrfs ] && command -v btrfs >/dev/null 2>&1; then
        printf '  жду, пока btrfs освободит место удалённых снимков…\n'
        timeout 600 btrfs subvolume sync / >/dev/null 2>&1 || true
        btrfs filesystem sync / >/dev/null 2>&1 || sync -f / >/dev/null 2>&1 || true
    fi
}
space_apply() {
    local want=$1 before now
    before=$(space_free)
    before=${before:-0}
    if [ "$before" -ge "$want" ]; then
        good "свободно $(size "$before") — места уже хватает, ничего не удаляю"
        return 0
    fi
    step "Освобождаю место на диске: свободно $(size "$before"), нужно $(size "$want")" \
        "— не хватает $(size $((want - before)))"
    step "Кэш pacman: оставляю только установленные версии пакетов (pacman -Sc)"
    pacman -Sc --noconfirm || warn "pacman -Sc не отработал"
    now=$(space_free)
    now=${now:-0}
    if [ "$now" -lt "$want" ] && command -v journalctl >/dev/null 2>&1; then
        step "Журнал systemd: оставляю последние 200 МБ"
        journalctl --vacuum-size=200M || warn "журнал не почистился"
        now=$(space_free)
        now=${now:-0}
    fi
    if [ "$now" -lt "$want" ] && command -v snapper >/dev/null 2>&1; then
        space_snapper "$want"
        now=$(space_free)
        now=${now:-0}
    fi
    good "освободил $(size $((now - before))), свободно $(size "$now")"
}
# space_short <bytes wanted>: / has less free than that
space_short() {
    local now
    now=$(space_free)
    [ "${now:-0}" -lt "$1" ]
}
# space_disk <folder>: the device its file system is on (a subvolume of / is on the same one)
space_disk() { df --output=source -- "$1" 2>/dev/null | tail -n 1; }
# space_ask <question>: yes from the user, typed after the question (no by default, no after 5 minutes)
space_ask() {
    local ans=
    printf '%s [y/N] ' "$1"
    drain
    read -r -t 300 ans || ans=
    case $ans in [yYдД]*) return 0 ;; esac
    return 1
}
space_snapper() {
    local want=$1 c conf list sub kinds k n t limit what
    for c in $(snapper --csvout list-configs --columns config 2>/dev/null | tail -n +2); do
        case $c in *[!A-Za-z0-9_.-]*|'') continue ;; esac
        space_short "$want" || return 0
        conf=$(snapper --csvout -c "$c" get-config 2>/dev/null)
        sub=$(sed -n 's/^SUBVOLUME,//p' <<<"$conf")
        if [ -n "$sub" ] && [ -d "$sub" ] && [ "$(space_disk "$sub")" != "$(space_disk /)" ]; then
            warn "снимки snapper ($c) лежат на другом диске — места на / они не освободят, не трогаю их"
            continue
        fi
        kinds=
        grep -qx 'NUMBER_CLEANUP,yes' <<<"$conf" && kinds=number
        grep -qx 'TIMELINE_CLEANUP,yes' <<<"$conf" && kinds="${kinds:+$kinds }timeline"
        if [ -z "$kinds" ]; then
            warn "снимки snapper ($c) он сам не чистит (TIMELINE_CLEANUP и NUMBER_CLEANUP не yes) — не трогаю их"
            continue
        fi
        list=$(snapper -c "$c" --csvout list --columns number,cleanup 2>/dev/null)
        n=0 t=0
        case " $kinds " in *" number "*) n=$(grep -c ',number$' <<<"$list") ;; esac
        case " $kinds " in *" timeline "*) t=$(grep -c ',timeline$' <<<"$list") ;; esac
        # only the kinds there are snapshots of (archinstall's config cleans up both, snap-pac's pairs are by number)
        kinds=
        [ "$n" -gt 0 ] && kinds=number
        [ "$t" -gt 0 ] && kinds="${kinds:+$kinds }timeline"
        [ -n "$kinds" ] || continue
        limit=$(sed -n 's/^NUMBER_LIMIT,//p' <<<"$conf")
        case $kinds in
            number) what=", по его настройке он держит до ${limit:-?}" ;;
            timeline) what=", это снимки по расписанию" ;;
            *) what=" — $n по номеру (держит до ${limit:-?}) и $t по расписанию" ;;
        esac
        printf '\n  Снимков snapper (%s), которые он удаляет сам: %s%s.\n' "$c" "$((n + t))" "$what"
        printf '  Они хранят старые версии файлов. Удалить лишние по его же правилам (snapper cleanup %s)?\n' \
            "${kinds/ / и }"
        if ! space_ask '  Откатиться к удалённым снимкам будет нельзя.'; then
            warn "снимки snapper ($c) не трогаю"
            continue
        fi
        step "Снимки snapper ($c): чищу по его правилам"
        for k in $kinds; do
            snapper -c "$c" cleanup "$k" || warn "snapper cleanup $k не отработал"
        done
        space_settle
        case " $kinds " in *" number "*) ;; *) continue ;; esac
        space_short "$want" || return 0
        n=$(snapper -c "$c" --csvout list --columns number,cleanup 2>/dev/null | grep -c ',number$')
        [ "${n:-0}" -gt 12 ] || continue
        space_limits "$c" "$conf"
    done
}
# space_lower <limit> <most>: snapper's number limit lowered to at most that ("50" → "10", "2-50" → "2-10", "20-50" →
# "10"); nothing when it is not above it already, or is not a limit snapper would take
space_lower() {
    local v=$1 most=$2 lo='' hi
    case $v in
        *-*) lo=${v%-*} hi=${v#*-} ;;
        *) hi=$v ;;
    esac
    case "$lo$hi" in ''|*[!0-9]*) return 0 ;; esac
    [ -n "$hi" ] && [ "$hi" -gt "$most" ] || return 0
    if [ -n "$lo" ] && [ "$lo" -lt "$most" ]; then printf '%s-%s\n' "$lo" "$most"; else printf '%s\n' "$most"; fi
}
# space_limits <config> <its get-config>: asks to lower its number limits to the last 10 snapshots and 5 important
# ones, saying the very values it will set; only what goes down, and nothing asked when nothing would
space_limits() {
    local c=$1 conf=$2 n i set=() keep shown
    n=$(space_lower "$(sed -n 's/^NUMBER_LIMIT,//p' <<<"$conf")" 10)
    i=$(space_lower "$(sed -n 's/^NUMBER_LIMIT_IMPORTANT,//p' <<<"$conf")" 5)
    [ -n "$n" ] && set+=("NUMBER_LIMIT=$n")
    [ -n "$i" ] && set+=("NUMBER_LIMIT_IMPORTANT=$i")
    [ ${#set[@]} -gt 0 ] || return 0
    shown=$(printf '%s, ' "${set[@]}")
    if [ -n "$n" ]; then
        keep="${n#*-} последних снимков"
        printf '\n  Места всё ещё мало. Оставить из снимков snapper (%s) только %s%s?\n' "$c" "$keep" \
            "${i:+ (и ${i#*-} важных)}"
    else
        keep="${i#*-} последних важных снимков"
        printf '\n  Места всё ещё мало. Оставить из важных снимков snapper (%s) только %s?\n' "$c" "${i#*-} последних"
    fi
    printf '  Дальше snapper будет держаться этого сам (%s).\n' "${shown%, }"
    if ! space_ask "  Откатиться к удалённым снимкам будет нельзя. Оставить $keep?"; then
        warn "снимки snapper ($c) больше не трогаю"
        return 0
    fi
    step "snapper ($c): оставляю $keep"
    if snapper -c "$c" set-config "${set[@]}"; then
        snapper -c "$c" cleanup number || warn "snapper cleanup не отработал"
        space_settle
    else
        warn "настройку snapper поменять не удалось"
    fi
}
space_verify() {
    local want=$1 least=${2:-$1} now
    now=$(space_free)
    now=${now:-0}
    if [ "$now" -ge "$want" ]; then
        good "свободно $(size "$now") — хватает"
        return 0
    fi
    if [ "$now" -ge "$least" ]; then
        warn "свободно $(size "$now"): для исправлений хватает, для обновления нужно около $(size "$want")" \
            "— не хватает $(size $((want - now)))"
        done_as=partial
        return 0
    fi
    bad "места всё ещё мало: свободно $(size "$now"), нужно хотя бы $(size "$least")" \
        "— не хватает $(size $((least - now)))"
    printf '  Больше всего обычно занимают:\n'
    if command -v snapper >/dev/null 2>&1; then printf '    снимки snapper: sudo snapper list\n'; fi
    if command -v timeshift >/dev/null 2>&1; then printf '    снимки Timeshift: sudo timeshift --list\n'; fi
    if command -v yay >/dev/null 2>&1; then printf '    кэш yay: ~/.cache/yay\n'; fi
    if command -v paru >/dev/null 2>&1; then printf '    кэш paru: ~/.cache/paru\n'; fi
    printf '    ненужные пакеты: pacman -Qdtq\n'
    printf '    файлы в домашней папке: du -sh ~/* | sort -h\n'
    failed_as=notenough
    return 1
}
"""
