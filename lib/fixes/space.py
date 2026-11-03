"""«Освободить место»: room on the root file system without touching anything the system needs.

When: the update stopped before it began because the disk was too small for it (res["space"]: the engine's
check before pacman's transaction, which counts what a snapper snapshot keeps), the disk ran full during the
update (ENOSPC in the output: engine.DISK_FULL), or other fixes are about to run on a nearly full disk (depmod
and mkinitcpio need room to write).

What, in order, each step only where it applies:
  1. pacman's cache down to the versions installed now (pacman -Sc): what a reinstall would need stays;
  2. the systemd journal down to its newest 200 MB;
  3. snapper's snapshots by its own clean-up rules (snapper cleanup number: what its timer does anyway);
  4. only if there is still too little room and the user says yes (a question in the terminal, no by default,
     no after 5 minutes): snapper's limits lowered to its last 10 snapshots (and 5 important ones), which it then
     keeps to; the snapshots it deletes cannot be rolled back to.
Nothing to put back: what goes is caches, old journal entries and, if the user agreed, old snapshots. Verified by
the free space it leaves; when that is still less than wanted, it says so (notenough) and what else takes room.
"""

import shutil

import engine
from fixes import GIB, fix

KEY = "space"
LOW = 512 * 2 ** 20          # below this free on / the other fixes get this one first
AFTER_FULL = 2 * GIB         # wanted free after the disk ran full during an update


def plan(ctx):
    want = None
    space = ctx.res.get("space") if isinstance(ctx.res.get("space"), dict) else None
    if space and isinstance(space.get("need"), int) and space["need"] > 0:
        want = space["need"]
    elif engine.DISK_FULL in [p for p in ctx.res.get("problems") or [] if isinstance(p, str)]:
        want = AFTER_FULL
    elif ctx.issues and (free() if free() is not None else LOW) < LOW:
        want = 1 * GIB
    if want is None:
        return []
    return [fix(KEY, "освободить место на диске", [ctx.su, "pacman", "-Sc"], [str(want)],
                how=f"{ctx.su} pacman -Sc; {ctx.su} journalctl --vacuum-size=200M")]


def free(path="/"):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


SCRIPT = r"""
# space <bytes wanted free on />
space_backup() { return 0; }
space_apply() {
    local want=$1 before after c n ans
    before=$(free_bytes /)
    step "Освобождаю место на диске: свободно $(size "$before"), нужно $(size "$want")"
    step "Кэш pacman: оставляю только установленные версии пакетов (pacman -Sc)"
    pacman -Sc --noconfirm || warn "pacman -Sc не отработал"
    if command -v journalctl >/dev/null 2>&1; then
        step "Журнал systemd: оставляю последние 200 МБ"
        journalctl --vacuum-size=200M || warn "журнал не почистился"
    fi
    if command -v snapper >/dev/null 2>&1; then
        for c in $(snapper --csvout list-configs --columns config 2>/dev/null | tail -n +2); do
            case $c in *[!A-Za-z0-9_.-]*|'') continue ;; esac
            step "Снимки snapper ($c): чищу по его же правилам (snapper cleanup number)"
            snapper -c "$c" cleanup number || warn "snapper cleanup не отработал"
        done
        space_settle
        if [ "$(free_bytes /)" -lt "$want" ]; then
            n=$(snapper -c root --csvout list --columns number,cleanup 2>/dev/null | grep -c ',number$')
            if [ "${n:-0}" -gt 12 ]; then
                printf '\n  Снимков snapper, которые он удаляет сам: %s — они держат старые версии файлов.\n' "$n"
                printf '  Можно оставить из них 10 последних (и 5 важных), и дальше snapper будет держаться этого\n'
                printf '  (NUMBER_LIMIT=10, NUMBER_LIMIT_IMPORTANT=5). Откатиться к удалённым снимкам будет нельзя.\n'
                printf '  Оставить 10 последних снимков? [y/N] '
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
                    *) warn "снимки snapper не трогаю" ;;
                esac
            fi
        fi
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
    local now
    now=$(free_bytes /)
    if [ "${now:-0}" -ge "$1" ]; then
        good "свободно $(size "$now") — хватает"
        return 0
    fi
    bad "места всё ещё мало: свободно $(size "${now:-0}"), нужно $(size "$1")"
    printf '  Больше всего обычно занимают: снимки snapper (sudo snapper list), кэш AUR (~/.cache/yay),\n'
    printf '  ненужные пакеты (pacman -Qdtq) и файлы в домашней папке (du -sh ~/* | sort -h).\n'
    failed_as=notenough
    return 1
}
"""
