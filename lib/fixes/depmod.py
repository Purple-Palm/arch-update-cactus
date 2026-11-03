"""«Собрать список модулей ядра»: depmod for a kernel whose module list is missing or whose depmod failed (a
full disk: "depmod: FATAL … No space left on device", the Chromebook that did not boot), and ldconfig with it
(the library cache failed the same way then). Without the module list mkinitcpio puts no module into the
initramfs and nothing finds the kernel's modules: the machine does not boot.

Kept first: the kernel's module lists (modules.dep, .alias, .symbols … and their .bin) and /etc/ld.so.cache;
put back if depmod or ldconfig fails or a list is still missing after them. The initramfs fix runs after this
one: an image built without the lists has no modules."""

import engine
from fixes import KVER, fix

LD_CACHE = "/etc/ld.so.cache"
KEY = "depmod"


def plan(ctx):
    kvers = []
    for i in ctx.of("depmod"):
        for k in [i.get("kver")] + list(i.get("kvers") or []):
            if isinstance(k, str) and KVER.fullmatch(k) and k not in kvers:
                kvers.append(k)
    if not kvers:
        return []
    shown = ", ".join(kvers)
    how = " && ".join([f"{ctx.su} depmod {k}" for k in kvers] + [f"{ctx.su} ldconfig"])
    return [fix(KEY, f"собрать список модулей ядра {shown} (depmod)", [ctx.su, "depmod", *kvers[:1]],
                [f"m:{engine.MODULES_DIR}", f"l:{LD_CACHE}", *kvers], how=how)]


SCRIPT = r"""
# depmod m:<modules folder> l:<library cache> <kernel version>…
DEPMOD_LISTS="modules.alias modules.alias.bin modules.builtin.alias.bin modules.builtin.bin modules.dep
modules.dep.bin modules.devname modules.softdep modules.symbols modules.symbols.bin modules.weakdep"
depmod_args() {
    local a
    depmod_dir=/usr/lib/modules depmod_ld=/etc/ld.so.cache depmod_kvers=()
    for a; do
        case $a in
            m:/*) depmod_dir=${a#m:} ;;
            l:/*) depmod_ld=${a#l:} ;;
            *[!A-Za-z0-9_.+~-]*|'') bad "странная версия ядра: $a"; return 1 ;;
            *) depmod_kvers+=("$a") ;;
        esac
    done
}
depmod_backup() {
    local k f files=()
    depmod_args "$@" || return 1
    for k in "${depmod_kvers[@]}"; do
        if [ ! -d "$depmod_dir/$k" ]; then bad "ядра $k нет ($depmod_dir/$k)"; return 1; fi
        for f in $DEPMOD_LISTS; do files+=("$depmod_dir/$k/$f"); done
    done
    files+=("$depmod_ld")
    room_for "${files[@]}" && keep "${files[@]}"
}
depmod_apply() {
    local k rc=0
    depmod_args "$@" || return 1
    for k in "${depmod_kvers[@]}"; do
        step "Список модулей ядра $k (depmod $k)"
        depmod "$k" || rc=1
    done
    step "Кэш библиотек (ldconfig)"
    ldconfig || rc=1
    return "$rc"
}
depmod_verify() {
    local k
    depmod_args "$@" || return 1
    for k in "${depmod_kvers[@]}"; do
        if [ ! -s "$depmod_dir/$k/modules.dep.bin" ]; then
            bad "у ядра $k всё ещё нет списка модулей"
            return 1
        fi
        good "список модулей ядра $k на месте"
    done
    nonempty "$depmod_ld"
}
"""
