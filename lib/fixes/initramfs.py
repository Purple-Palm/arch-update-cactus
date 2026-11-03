"""«Собрать initramfs заново», one fix per kernel: an initramfs (or UKI) of that kernel missing, empty or broken,
mkinitcpio failed or warned that the image may not be complete ("errors were encountered during the build", "No
modules were added to the image": the Chromebook that did not boot), or that kernel's module lists / a DKMS module
for it were just rebuilt (an image built before them lacks modules).

mkinitcpio -p <that kernel's preset> (only kernels installed with a preset: a preset of a kernel no longer there
would fail for nothing). One fix per kernel, so that a kernel whose image cannot be built (linux-lts with no NVIDIA
module for it, while MODULES lists nvidia) leaves the repair of another one in place. Not for a kernel the kernel
fix reinstalls in this same run: pacman's own hook builds its images then (and without its kernel file mkinitcpio
cannot).

Kept first: every image and UKI of the preset (in /tmp, room checked first), and the "<image>.tmp" mkinitcpio
writes first when it guesses there is room for it and leaves behind when the build fails (kept as missing: it goes
first on a rollback, so that its room is there for the copies). An image that is no good anyway (missing modules,
cut short, for another kernel) is removed before the build (its copy is kept): mkinitcpio then writes the new one
in its place with all the room there is — on a small /boot the old one without modules, 13 MB, and the new one, 24
MB, do not fit side by side. Verified: mkinitcpio ended well and said nothing of errors, and every image is there,
built for its kernel and with that kernel's module list in it (lsinitcpio); every UKI is there. Otherwise all of
them back as they were, each checked against its copy; when a file system ran out of room for the build, the fix
says which one."""

import glob
import os

import engine
from fixes import KVER, NAME, fix

KEY = "initramfs"


def kvers_of(f):
    """The kernel versions a planned depmod or DKMS fix rebuilds something for."""
    if f["key"] == "depmod":
        return [a for a in f["args"] if not a.startswith(("m:", "l:"))]
    if f["key"] == "dkms":
        return [a.split(":", 1)[1] for a in f["args"] if ":" in a]
    return []


def plan(ctx):
    kinds = ctx.kinds()
    after = [f for f in ctx.planned if f["key"] in ("depmod", "dkms")]
    if not (kinds & {"image", "initcpio"} or after):
        return []
    if not glob.glob(os.path.join(engine.PRESET_DIR, "*.preset")) or not ctx.presets():
        return []                               # booster, dracut…: not ours to rebuild
    presets = ctx.presets()
    package = {k: p for p, k in ctx.kernels().items()}
    pkgs, every = set(), "initcpio" in kinds    # mkinitcpio failed in the update: it does not say for which kernel
    for i in ctx.of("image"):
        p = i.get("pkg")
        if isinstance(p, str) and NAME.fullmatch(p):
            pkgs.add(p)
        else:
            every = True
    if every:
        pkgs |= set(presets)
    for f in after:
        if f["key"] == "depmod":                # the kernel reinstall's hook builds its images after its depmod
            pkgs |= {package[k] for k in kvers_of(f) if KVER.fullmatch(k) and k in package}
    pkgs -= ctx.scopes("kernel")                # pacman's hook builds them, once the kernel file is back
    for f in after:
        if f["key"] == "dkms":                  # after the module is built, whatever came before it
            pkgs |= {package[k] for k in kvers_of(f) if KVER.fullmatch(k) and k in package}
    return [fix(KEY, f"собрать заново initramfs ядра {p} (mkinitcpio)", [ctx.su, "mkinitcpio", "-p", p],
                [f"p:{p}"] + [s for s in ctx.image_specs({p}) if not s.startswith("k:")], scope=p)
            for p in sorted(pkgs) if p in presets]


SCRIPT = r"""
# initramfs p:<preset>… i:<image spec>… u:<unified kernel image>…
initramfs_backup() {
    local a f files=()
    for a; do
        case $a in
            i:*) f=${a#i:*:*:} ;;
            u:*) f=${a#u:} ;;
            *) continue ;;
        esac
        files+=("$f" "$f.tmp")
    done
    room_for "${files[@]}" && keep "${files[@]}" && : >"$work/mkinitcpio.out"
}
initramfs_apply() {
    local a f p why out="$work/mkinitcpio.out"
    # an image that is no good anyway goes first (its copy is kept): mkinitcpio then writes the new one in its
    # place with all the room there is (it writes beside the old one while it guesses there is room for 1.25 times
    # the old one's size; a new one with modules is twice the size of an old one without). Never a UKI.
    for a; do
        case $a in i:*) ;; *) continue ;; esac
        f=${a#i:*:*:}
        [ -e "$f" ] || continue
        why=$(image_trouble "$a") && continue
        step "Убираю негодный $f, чтобы новому initramfs хватило места (его копия сохранена)"
        warn "$why"
        rm -f -- "$f" || return 1
    done
    for a; do
        case $a in p:*) ;; *) continue ;; esac
        p=${a#p:}
        case $p in *[!A-Za-z0-9_.+@-]*|'') bad "странное имя пресета: $p"; return 1 ;; esac
        step "Собираю initramfs заново: mkinitcpio -p $p"
        : >"$out"
        mkinitcpio -p "$p" 2>&1 | tee -a "$out"
        # the first preset that failed ends it: no more images rebuilt on a disk that may be full
        if [ "${PIPESTATUS[0]}" -ne 0 ]; then
            bad "mkinitcpio -p $p закончил с ошибкой"
            initramfs_full "$out" "$@"
            return 1
        fi
        if grep -qiE "$INITCPIO_TROUBLE" "$out"; then
            bad "mkinitcpio сообщил об ошибках — с таким initramfs система может не загрузиться"
            initramfs_full "$out" "$@"
            return 1
        fi
    done
}
# initramfs_full <mkinitcpio's output> <specs…>: after a build that failed, whether a file system ran out of room for
# it: the images' or mkinitcpio's build folder's (in /tmp). Looked at right away, before the copies give the room
# back: under 1 MiB free there, or else the one "No space left on device" names (mkinitcpio removes its build folder
# as it stops), or the one with the least room. Said, and noted with the fix (full:<mount point>).
initramfs_full() {
    local out=$1 a d m avail low='' lowest='' least='' dirs=()
    shift
    for a; do
        case $a in
            i:*) dirs+=("$(dirname -- "${a#i:*:*:}")") ;;
            u:*) dirs+=("$(dirname -- "${a#u:}")") ;;
        esac
    done
    dirs+=("${TMPDIR:-/tmp}")
    for d in "${dirs[@]}"; do
        avail=$(free_bytes "$d")
        [ -n "$avail" ] || continue
        if [ "$avail" -lt 1048576 ]; then low=$d; break; fi
        if [ -z "$least" ] || [ "$avail" -lt "$least" ]; then least=$avail lowest=$d; fi
    done
    if [ -z "$low" ] && grep -qi 'No space left on device' "$out"; then
        for d in "${dirs[@]}"; do
            if grep -i 'No space left on device' "$out" | grep -qF -- "$d/"; then low=$d; break; fi
        done
        low=${low:-$lowest}
    fi
    [ -n "$low" ] || return 0
    m=$(stat -c %m -- "$low" 2>/dev/null) || m=$low
    avail=$(free_bytes "$low")
    bad "на $m не хватило места для initramfs: свободно $(size "${avail:-0}")"
    case $m in /*[[:space:]]*|'') ;; /*) note_extra="${note_extra:+$note_extra }full:$m" ;; esac
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
