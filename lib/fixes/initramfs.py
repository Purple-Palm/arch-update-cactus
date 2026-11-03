"""«Собрать initramfs заново»: an initramfs (or UKI) missing, empty or broken, mkinitcpio failed or warned that
the image may not be complete ("errors were encountered during the build", "No modules were added to the image":
the Chromebook that did not boot), or the module lists / a DKMS module were just rebuilt (an image built before
them lacks modules).

mkinitcpio -p <preset> for every installed kernel with a preset (only those: a preset of a kernel no longer
installed would fail mkinitcpio -P for nothing). Kept first: every image and UKI of those presets (in /tmp, room
checked first). Verified: mkinitcpio ended well and said nothing of errors, and every image is there, built for its
kernel and with that kernel's module list in it (lsinitcpio); every UKI is there. Otherwise all of them back as
they were, each checked against its copy."""

import glob
import os

import engine
from fixes import fix

KEY = "initramfs"


def plan(ctx):
    kinds = ctx.kinds()
    after = any(f["key"] in ("depmod", "dkms") for f in ctx.planned)
    if not (kinds & {"image", "initcpio"} or after):
        return []
    if not glob.glob(os.path.join(engine.PRESET_DIR, "*.preset")) or not ctx.presets():
        return []                               # booster, dracut…: not ours to rebuild
    pkgs = sorted(ctx.presets())
    args = [f"p:{p}" for p in pkgs] + [s for s in ctx.image_specs() if not s.startswith("k:")]
    return [fix(KEY, "собрать initramfs заново (mkinitcpio)", [ctx.su, "mkinitcpio", "-P"], args,
                how=" && ".join(f"{ctx.su} mkinitcpio -p {p}" for p in pkgs))]


SCRIPT = r"""
# initramfs p:<preset>… i:<image spec>… u:<unified kernel image>…
INITCPIO_TROUBLE='errors were encountered during the build|No modules were added to the image|image generation FAILED|No space left on device'
initramfs_backup() {
    local a files=()
    for a; do
        case $a in
            i:*) files+=("${a#i:*:*:}") ;;
            u:*) files+=("${a#u:}") ;;
        esac
    done
    room_for "${files[@]}" && keep "${files[@]}"
}
initramfs_apply() {
    local a p rc=0
    : >"$work/mkinitcpio.log"
    for a; do
        case $a in p:*) ;; *) continue ;; esac
        p=${a#p:}
        case $p in *[!A-Za-z0-9_.+@-]*|'') bad "странное имя пресета: $p"; return 1 ;; esac
        step "Собираю initramfs заново: mkinitcpio -p $p"
        mkinitcpio -p "$p" 2>&1 | tee -a "$work/mkinitcpio.log"
        if [ "${PIPESTATUS[0]}" -ne 0 ]; then
            bad "mkinitcpio -p $p закончил с ошибкой"
            rc=1
        fi
    done
    if grep -qiE "$INITCPIO_TROUBLE" "$work/mkinitcpio.log"; then
        bad "mkinitcpio сообщил об ошибках — с таким initramfs система может не загрузиться"
        rc=1
    fi
    return "$rc"
}
initramfs_verify() {
    local a ok=0
    step "Проверяю, что получилось"
    for a; do
        case $a in
            i:*) image_ok "$a" || ok=1 ;;
            u:*) nonempty "${a#u:}" || ok=1 ;;
        esac
    done
    return "$ok"
}
"""
