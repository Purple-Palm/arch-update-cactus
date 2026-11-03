"""«Собрать модули DKMS», one fix per kernel: a DKMS module (the NVIDIA driver on the main PC) not built for an
installed kernel — the pacman hook never fails the update for it. dkms autoinstall -k <kernel>; verified with dkms
status (the module installed for that kernel). A build that fails installs nothing: nothing to put back. The
initramfs fix of that kernel runs after this one (the image takes the new module in); a kernel whose module does
not build holds up only its own initramfs, not another kernel's."""

from fixes import KVER, NAME, fix

KEY = "dkms"


def plan(ctx):
    by_kver = {}                                # kernel version → [module:kernel version]
    for i in ctx.of("dkms"):
        m = i.get("module")
        for k in i.get("kvers") or []:
            if isinstance(m, str) and NAME.fullmatch(m) and isinstance(k, str) and KVER.fullmatch(k) \
                    and f"{m}:{k}" not in by_kver.get(k, []):
                by_kver.setdefault(k, []).append(f"{m}:{k}")
    if not by_kver:
        return []
    try:
        package = {k: p for p, k in ctx.kernels().items()}
    except Exception:                           # no telling which package: a fix for the whole machine then
        package = {}
    out = []
    for k, pairs in by_kver.items():
        what = "драйвер NVIDIA" if any(p.startswith("nvidia:") for p in pairs) else "модули DKMS"
        out.append(fix(KEY, f"собрать {what} под ядро {k}", [ctx.su, "dkms", "autoinstall", "-k", k], pairs,
                       scope=package.get(k)))
    return out


SCRIPT = r"""
# dkms <module>:<kernel version>…
dkms_backup() { return 0; }
dkms_apply() {
    local a k seen=" " rc=0
    for a; do
        k=${a#*:}
        case $seen in *" $k "*) continue ;; esac
        seen="$seen$k "
        step "Собираю модули DKMS под ядро $k (dkms autoinstall -k $k)"
        dkms autoinstall -k "$k" || rc=1
    done
    return "$rc"
}
dkms_verify() {
    local a m k
    for a; do
        m=${a%%:*}
        k=${a#*:}
        if dkms status -m "$m" -k "$k" 2>/dev/null | grep -q ': installed'; then
            good "$m собран под ядро $k"
        else
            bad "$m так и не собран под ядро $k"
            return 1
        fi
    done
}
"""
