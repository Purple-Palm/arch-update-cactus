"""«Освободить место»: room on the root file system without touching anything the system needs.

When: the update stopped before it began because the disk was too small for it (res["space"]: the engine's
check before pacman's transaction, which counts what a snapper snapshot keeps), the disk ran full during the
update (ENOSPC in the output: engine.DISK_FULL), or other fixes are about to run on a nearly full disk (depmod
and mkinitcpio need room to write). Only while / really has less free than wanted: a full disk elsewhere (/tmp of
an AUR build, a /boot partition) is not freed by this, and nothing is deleted for nothing.

Two amounts: what is wanted (the update's need, or 2 GiB after a disk that ran full) and the least the fixes after
this one need to work (LOW). Ending above the least but below the wanted is "partial": the fixes after it still
run (an unbootable machine is mended first), and the owner is told the update needs more room.

What, in order, each step only where it applies:
  1. pacman's cache down to the versions installed now (pacman -Sc): what a reinstall would need stays;
  2. the systemd journal down to its newest 200 MB;
  3. snapper's snapshots, only for a snapper config that cleans up by number by itself (NUMBER_CLEANUP=yes), and
     only when the user says yes in the terminal (no by default, no after 5 minutes, keys pressed before the
     question do not count): first by its own limits (snapper cleanup number), then, if there is still too little
     room and the user says yes again, with its limits lowered to the last 10 snapshots (and 5 important ones),
     which it keeps to from then on. Deleted snapshots cannot be rolled back to.
Nothing to put back: what goes is caches, old journal entries and, if the user agreed, old snapshots. Verified by
the free space it leaves.
"""

import shutil

import engine
from fixes import GIB, fix

KEY = "space"
LOW = 512 * 2 ** 20          # below this free on / the other fixes get this one first; and the least they need
AFTER_FULL = 2 * GIB         # wanted free after the disk ran full during an update


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
    return [fix(KEY, "освободить место на диске", [su, "pacman", "-Sc"], [str(want)],
                how=f"{su} pacman -Sc; {su} journalctl --vacuum-size=200M; снимки snapper — только если ответишь «да»")]


def finish(ctx):
    """After all fixes are planned: the least this one has to leave is what the fixes after it need (LOW), or the
    whole amount wanted when it is the only one (room for the update)."""
    for f in ctx.planned:
        if f["key"] == KEY and len(f["args"]) == 1:
            want = int(f["args"][0])
            alone = all(g["key"] == KEY for g in ctx.planned)
            f["args"].append(str(want if alone else min(want, LOW)))


def free(path="/"):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


SCRIPT = r"""
# space <bytes wanted free on /> <the least the fixes after this one need>
space_backup() { return 0; }
space_apply() {
    local want=$1 before after c n ans limit
    before=$(free_bytes /)
    step "Освобождаю место на диске: свободно $(size "$before"), нужно $(size "$want")"
    step "Кэш pacman: оставляю только установленные версии пакетов (pacman -Sc)"
    pacman -Sc --noconfirm || warn "pacman -Sc не отработал"
    if command -v journalctl >/dev/null 2>&1; then
        step "Журнал systemd: оставляю последние 200 МБ"
        journalctl --vacuum-size=200M || warn "журнал не почистился"
    fi
    if command -v snapper >/dev/null 2>&1 && [ "$(free_bytes /)" -lt "$want" ]; then
        for c in $(snapper --csvout list-configs --columns config 2>/dev/null | tail -n +2); do
            case $c in *[!A-Za-z0-9_.-]*|'') continue ;; esac
            if ! snapper --csvout -c "$c" get-config 2>/dev/null | grep -qx 'NUMBER_CLEANUP,yes'; then
                warn "снимки snapper ($c) он сам не чистит (NUMBER_CLEANUP не yes) — не трогаю их"
                continue
            fi
            n=$(snapper -c "$c" --csvout list --columns number,cleanup 2>/dev/null | grep -c ',number$')
            limit=$(snapper --csvout -c "$c" get-config 2>/dev/null | sed -n 's/^NUMBER_LIMIT,//p')
            [ "${n:-0}" -gt 0 ] || continue
            printf '\n  Снимков snapper (%s), которые он удаляет сам: %s, по его настройке он держит до %s.\n' \
                "$c" "$n" "${limit:-?}"
            printf '  Они хранят старые версии файлов. Удалить лишние по его же правилам (snapper cleanup number)?\n'
            printf '  Откатиться к удалённым снимкам будет нельзя. [y/N] '
            drain
            ans=
            read -r -t 300 ans || ans=
            case $ans in
                [yYдД]*) ;;
                *) warn "снимки snapper ($c) не трогаю"; continue ;;
            esac
            step "Снимки snapper ($c): чищу по его правилам"
            snapper -c "$c" cleanup number || warn "snapper cleanup не отработал"
            space_settle
            [ "$c" = root ] || continue
            [ "$(free_bytes /)" -lt "$want" ] || continue
            n=$(snapper -c root --csvout list --columns number,cleanup 2>/dev/null | grep -c ',number$')
            [ "${n:-0}" -gt 12 ] || continue
            printf '\n  Места всё ещё мало. Оставить из снимков snapper только 10 последних (и 5 важных)?\n'
            printf '  Дальше snapper будет держаться этого сам (NUMBER_LIMIT=10, NUMBER_LIMIT_IMPORTANT=5).\n'
            printf '  Откатиться к удалённым снимкам будет нельзя. Оставить 10 последних снимков? [y/N] '
            drain
            ans=
            read -r -t 300 ans || ans=
            case $ans in
                [yYдД]*)
                    step "snapper: оставляю 10 последних снимков"
                    if snapper -c root set-config NUMBER_LIMIT=10 NUMBER_LIMIT_IMPORTANT=5; then
                        snapper -c root cleanup number || warn "snapper cleanup не отработал"
                        space_settle
                    else
                        warn "настройку snapper поменять не удалось"
                    fi
                    ;;
                *) warn "снимки snapper больше не трогаю" ;;
            esac
        done
    fi
    after=$(free_bytes /)
    good "освободил $(size $((after - before))), свободно $(size "$after")"
}
# btrfs frees what deleted snapshots held a little later, in the background: wait for it (at most 10 minutes)
space_settle() {
    if [ "$(stat -f -c %T / 2>/dev/null)" = btrfs ] && command -v btrfs >/dev/null 2>&1; then
        printf '  жду, пока btrfs освободит место удалённых снимков…\n'
        timeout 600 btrfs subvolume sync / >/dev/null 2>&1 || true
    fi
}
space_verify() {
    local want=$1 least=${2:-$1} now
    now=$(free_bytes /)
    if [ "${now:-0}" -ge "$want" ]; then
        good "свободно $(size "$now") — хватает"
        return 0
    fi
    if [ "${now:-0}" -ge "$least" ]; then
        warn "свободно $(size "$now"): для исправлений хватает, для обновления нужно около $(size "$want")"
        done_as=partial
        return 0
    fi
    bad "места всё ещё мало: свободно $(size "${now:-0}"), нужно хотя бы $(size "$least")"
    printf '  Больше всего обычно занимают: снимки snapper (sudo snapper list), кэш AUR (~/.cache/yay),\n'
    printf '  ненужные пакеты (pacman -Qdtq) и файлы в домашней папке (du -sh ~/* | sort -h).\n'
    failed_as=notenough
    return 1
}
"""
