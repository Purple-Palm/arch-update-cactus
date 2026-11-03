"""«Собрать модули DKMS»: a DKMS module (the NVIDIA driver on the main PC) not built for an installed kernel —
the pacman hook never fails the update for it. dkms autoinstall -k <kernel>; verified with dkms status (the
module installed for that kernel). A build that fails installs nothing: nothing to put back. The initramfs fix
runs after this one (the image takes the new module in)."""

from fixes import KVER, NAME, fix

KEY = "dkms"


def plan(ctx):
    pairs = []
    for i in ctx.of("dkms"):
        m = i.get("module")
        for k in i.get("kvers") or []:
            if isinstance(m, str) and NAME.fullmatch(m) and isinstance(k, str) and KVER.fullmatch(k) \
                    and f"{m}:{k}" not in pairs:
                pairs.append(f"{m}:{k}")
    if not pairs:
        return []
    kvers = list(dict.fromkeys(p.split(":", 1)[1] for p in pairs))
    nvidia = any(p.startswith("nvidia:") for p in pairs)
    what = "драйвер NVIDIA" if nvidia else "модули DKMS"
    return [fix(KEY, f"собрать {what} под ядро {', '.join(kvers)}", [ctx.su, "dkms", "autoinstall", "-k", kvers[0]],
                pairs, how=" && ".join(f"{ctx.su} dkms autoinstall -k {k}" for k in kvers))]


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
