"""«Смонтировать раздел загрузки»: boot partitions from fstab (/boot, /efi, /boot/efi) that are not mounted (the
update refuses to start then: the kernel would be written into an empty folder). One fix for all of them: mount
<target> as fstab says, each in turn; verified with mountpoint. Nothing to put back: a mount that fails changes
nothing."""

from fixes import BOOT_TARGETS, fix

KEY = "mount"


def plan(ctx):
    targets = [t for t in dict.fromkeys(i.get("target") for i in ctx.of("unmounted")) if t in BOOT_TARGETS]
    if not targets:
        return []
    shown = targets[0] if len(targets) == 1 else ", ".join(targets[:-1]) + " и " + targets[-1]
    return [fix(KEY, f"смонтировать {shown}", [ctx.su, "mount", targets[0]], targets,
                how=" && ".join(f"{ctx.su} mount {t}" for t in targets))]


SCRIPT = r"""
# mount <target>…
mount_backup() { return 0; }
mount_apply() {
    local t
    for t; do
        case $t in /boot|/efi|/boot/efi) ;; *) bad "$t — не раздел загрузки"; return 1 ;; esac
        step "Монтирую $t (как написано в /etc/fstab)"
        if mountpoint -q -- "$t"; then good "$t уже смонтирован"; continue; fi
        mount -- "$t" || return 1
    done
}
mount_verify() {
    local t
    for t; do
        if mountpoint -q -- "$t"; then good "$t смонтирован"; else bad "$t так и не смонтирован"; return 1; fi
    done
}
"""
