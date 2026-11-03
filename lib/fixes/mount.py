"""«Смонтировать раздел загрузки»: a boot partition from fstab (/boot, /efi, /boot/efi) that is not mounted
(the update refuses to start then: the kernel would be written into an empty folder). mount <target> as fstab
says; verified with mountpoint. Nothing to put back: a mount that fails changes nothing."""

from fixes import BOOT_TARGETS, fix

KEY = "mount"


def plan(ctx):
    targets = [t for t in dict.fromkeys(i.get("target") for i in ctx.of("unmounted")) if t in BOOT_TARGETS]
    return [fix(KEY, f"смонтировать {t}", [ctx.su, "mount", t], [t]) for t in targets]


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
