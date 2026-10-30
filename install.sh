#!/usr/bin/env bash
# Arch Update by Cactus — installer.
#
# First install on a machine (Arch Linux, as your normal user, not root):
#   curl -fsSL https://raw.githubusercontent.com/Purple-Palm/arch-update-cactus/stable/install.sh | bash
#
#   install.sh             install or repair: packages, files, tray icon, timers (asks before installing)
#   install.sh --sync      refresh the installed files after a self-update (no packages, no questions)
#   install.sh --check     self-test of this checkout (used before a self-update is accepted)
#   install.sh --uninstall remove the links, menu entry and timer (logs and settings stay)
#
# Everything lives in one git checkout, ~/.local/share/arch-update-cactus, of the tag "stable"
# (CI moves it to each tested commit of main); ~/.local/bin entries are symlinks into it, so a
# self-update replaces the whole thing at once.
set -euo pipefail

REPO=Purple-Palm/arch-update-cactus
TAG=stable
DEST=$HOME/.local/share/arch-update-cactus
DEST_REAL=$(realpath -m "$DEST")              # compared with this script's real path
BIN=$HOME/.local/bin
APPS=$HOME/.local/share/applications
UNITS=$HOME/.config/systemd/user
STATE=$HOME/.local/state/arch-update-auto
BACKUP=$STATE/backup-$(date +%F_%H-%M-%S)
PROGRAMS=(arch-update-launcher arch-update-ui arch-update-auto arch-update-cactus-sync)
MENU_NEW=0

say() { printf '\e[1;36m::\e[0m %s\n' "$*"; }
warn() { printf '\e[1;33m!!\e[0m %s\n' "$*" >&2; }
die() {
	printf '\e[1;31m✗\e[0m %s\n' "$*" >&2
	exit 1
}

# Where this script lives; empty when it is piped from curl.
SRC=${BASH_SOURCE[0]-}
ROOT=

# ── bootstrap: piped from curl, or started from some other copy → work from the real checkout ──

bootstrap() {
	[[ $EUID -ne 0 ]] || die "запусти от своего пользователя, не от root (sudo спросится сам, когда понадобится)"
	command -v pacman >/dev/null || die "это не Arch Linux: нет pacman"
	[[ ! -L $DEST ]] || die "$DEST — ссылка (на копию для разработки?), её не трогаю"
	if ! command -v git >/dev/null; then
		say "нужен git — ставлю"
		{ sudo pacman -S --needed git; } </dev/tty
	fi
	if [[ -d $DEST/.git ]]; then
		git -C "$DEST" fetch --quiet origin "$TAG"
		git -C "$DEST" reset --quiet --hard FETCH_HEAD
	else
		say "скачиваю Arch Update by Cactus в $DEST"
		git -c advice.detachedHead=false clone --quiet --branch "$TAG" "https://github.com/$REPO.git" "$DEST"
	fi
	if (exec </dev/tty) 2>/dev/null; then
		exec bash "$DEST/install.sh" "$@" </dev/tty   # curl | bash took stdin: give the questions a keyboard back
	fi
	exec bash "$DEST/install.sh" "$@"
}

# ── pieces ──

# Replace DST with a symlink to SRC. A real file found there (an older hand-made install) is
# moved to the backup folder first, never deleted.
link() {
	local src=$1 dst=$2
	[[ -L $dst && $(readlink "$dst") == "$src" ]] && return 0
	if [[ -e $dst && ! -L $dst ]]; then
		backup "$dst"
	fi
	mkdir -p "$(dirname "$dst")"
	ln -sfn "$src" "$dst"
}

# Write CONTENT (stdin) to DST if it differs. Returns 0 when the file changed.
put() {
	local dst=$1 tmp
	tmp=$(mktemp)
	cat >"$tmp"
	if cmp -s "$tmp" "$dst"; then
		rm -f "$tmp"
		return 1
	fi
	mkdir -p "$(dirname "$dst")"
	chmod 644 "$tmp"
	mv -f "$tmp" "$dst"
}

# Move a file or folder into this run's backup folder, keeping its path below $HOME.
backup() {
	local rel=${1#"$HOME"/}
	mkdir -p "$BACKUP/$(dirname "$rel")"
	mv "$1" "$BACKUP/$rel"
	say "старый ~/$rel сохранён в ${BACKUP/#$HOME/\~}/"
}

# yay first: the auto-answers know its questions best (paru and pikaur work, but ask more of you).
aur_helper() {
	local h
	for h in yay paru pikaur; do
		command -v "$h" >/dev/null && { echo "$h"; return 0; }
	done
	return 1
}

install_files() {
	local p changed=0
	for p in "${PROGRAMS[@]}"; do
		link "$ROOT/bin/$p" "$BIN/$p"
	done
	# The engine used to live in ~/.local/lib/arch-update-auto; the programs now find it in the checkout.
	if [[ -d $HOME/.local/lib/arch-update-auto && ! -L $HOME/.local/lib/arch-update-auto ]]; then
		backup "$HOME/.local/lib/arch-update-auto"
	fi
	# Menu entry. It overrides arch-update's own one, so the tray icon, its notification
	# and any panel pin all open our launcher.
	[[ -e $APPS/arch-update.desktop ]] || MENU_NEW=1
	sed "s|@LAUNCHER@|$BIN/arch-update-launcher|" "$ROOT/share/arch-update.desktop.in" |
		put "$APPS/arch-update.desktop" && changed=1
	# Konsole profile (copies: Konsole rewrites profiles it is asked to edit).
	put "$HOME/.local/share/konsole/ArchUpdate.profile" <"$ROOT/share/konsole/ArchUpdate.profile" || true
	put "$HOME/.local/share/konsole/ArchUpdate.colorscheme" <"$ROOT/share/konsole/ArchUpdate.colorscheme" || true
	# Self-update timer.
	local units=0
	put "$UNITS/arch-update-cactus-sync.service" <"$ROOT/share/systemd/arch-update-cactus-sync.service" && units=1
	put "$UNITS/arch-update-cactus-sync.timer" <"$ROOT/share/systemd/arch-update-cactus-sync.timer" && units=1
	if systemctl --user show-environment >/dev/null 2>&1; then
		((units)) && systemctl --user daemon-reload
		systemctl --user is-enabled --quiet arch-update-cactus-sync.timer 2>/dev/null ||
			systemctl --user enable --now --quiet arch-update-cactus-sync.timer
	fi
	if ((changed)) && command -v update-desktop-database >/dev/null; then
		update-desktop-database --quiet "$APPS" 2>/dev/null || true
	fi
	return 0
}

# Every step decides (set -e is off inside a function called from "||").
self_check() {
	local f
	for f in install.sh bin/arch-update-launcher bin/arch-update-cactus-sync lib/demo-update; do
		bash -n "$ROOT/$f" || return 1
	done
	python3 -m py_compile "$ROOT/bin/arch-update-ui" "$ROOT/bin/arch-update-auto" "$ROOT/lib/engine.py" \
		"$ROOT/lib/games.py" || return 1
	python3 "$ROOT/bin/arch-update-ui" --self-test || return 1
}

# Build and install one AUR package with plain makepkg (asks for sudo itself).
makepkg_aur() {
	local tmp
	tmp=$(mktemp -d)
	git clone --quiet "https://aur.archlinux.org/$1.git" "$tmp/$1"
	(cd "$tmp/$1" && makepkg -si --needed)
	rm -rf "$tmp"
}

install_packages() {
	local want=(git python python-pexpect pacman-contrib libnotify ttf-hack) missing=() p cp
	for p in "${want[@]}"; do
		pacman -Q "$p" >/dev/null 2>&1 || missing+=("$p")
	done
	# ✓ ✗ ⚠ ✦ and the spinner come from fallback fonts (Hack lacks them)
	for cp in 2713 2717 26a0 2726 280b; do
		if [[ -z $(fc-list ":charset=$cp" family 2>/dev/null) ]]; then
			missing+=(noto-fonts)
			break
		fi
	done
	if ! "$ROOT/bin/arch-update-launcher" --terminal >/dev/null; then
		missing+=(kitty)
	fi
	if ((${#missing[@]})); then
		say "ставлю пакеты: ${missing[*]}"
		sudo pacman -S --needed "${missing[@]}" ||
			die "pacman не смог поставить пакеты. Если он не нашёл их на зеркале — сначала обнови систему: sudo pacman -Syu"
	fi
	# AUR packages: yay, as on the main machine (the auto-answers know its questions best)
	local helper
	if ! helper=$(aur_helper); then
		say "ставлю yay (помощник для пакетов из AUR)"
		sudo pacman -S --needed base-devel
		makepkg_aur yay-bin
		helper=yay
	fi
	command -v arch-update >/dev/null && return 0
	local pkg=arch-update
	[[ $(uname -m) == x86_64 ]] && pkg=arch-update-bin      # prebuilt; elsewhere it is built from source
	say "ставлю $pkg из AUR через $helper"
	"$helper" -S --needed "$pkg"
	command -v arch-update >/dev/null || die "arch-update не установился"
}

setup_arch_update() {
	local conf=$HOME/.config/arch-update/arch-update.conf helper
	if [[ ! -f $conf ]]; then
		arch-update --gen-config >/dev/null
		# As on the main machine: two old versions stay in the package cache; yay for the AUR.
		sed -i -e 's/^#KeepOldPackages=.*/KeepOldPackages=2/' "$conf"
		if helper=$(aur_helper); then
			sed -i "s/^#AURHelper=.*/AURHelper=$helper/" "$conf"
		fi
	fi
	if grep -qE '^[[:space:]]*NoVersion' "$conf"; then
		warn "в $conf включён NoVersion: без версий апдейтер не увидит список обновлений — закомментируй его"
	fi
	[[ -f $HOME/.config/autostart/arch-update-tray.desktop ]] || arch-update --tray --enable >/dev/null || true
	systemctl --user enable --now --quiet arch-update.timer 2>/dev/null || true
	# The tray picks the menu entry's file once, when it starts: one that started before our
	# entry existed keeps opening the plain arch-update until it is restarted.
	local unit='app-arch\x2dupdate\x2dtray@autostart.service'
	if pgrep -xu "$USER" arch-update-tra >/dev/null; then
		if ((MENU_NEW)); then
			if systemctl --user --quiet is-active "$unit" 2>/dev/null; then
				systemctl --user restart "$unit"
			else
				pkill -xu "$USER" arch-update-tra || true
				sleep 1
				setsid -f arch-update --tray >/dev/null 2>&1 </dev/null || true
			fi
		fi
	else
		setsid -f arch-update --tray >/dev/null 2>&1 </dev/null || true
	fi
}

report() {
	local term de=${XDG_CURRENT_DESKTOP:-?}
	term=$("$ROOT/bin/arch-update-launcher" --terminal 2>/dev/null || echo "не найден")
	say "готово: Arch Update by Cactus $(git -C "$ROOT" log -1 --format='%h от %cd' --date=format:%d.%m.%Y)"
	echo "   окружение: $de · терминал: $term · AUR: $(aur_helper || echo нет)"
	echo "   запуск: иконка Arch-Update в трее или меню приложений; демо: ~/.local/bin/arch-update-launcher --demo"
	echo "   новые версии приезжают сами (раз в час, после входа и при запуске); проверить сейчас:"
	echo "   ~/.local/bin/arch-update-cactus-sync"
	if [[ $de == *GNOME* ]] && ! pacman -Q gnome-shell-extension-appindicator >/dev/null 2>&1; then
		warn "в GNOME значок в трее появится только с расширением AppIndicator:"
		warn "  sudo pacman -S gnome-shell-extension-appindicator, затем перелогинься и включи его в «Расширениях»"
	fi
	if [[ $de == "?" ]]; then
		warn "окружение без автозапуска? добавь в автозапуск своего WM: sleep 3 && arch-update --tray"
	fi
}

uninstall() {
	local p
	if systemctl --user show-environment >/dev/null 2>&1; then
		systemctl --user disable --now --quiet arch-update-cactus-sync.timer 2>/dev/null || true
		systemctl --user clean --what=state arch-update-cactus-sync.timer 2>/dev/null || true
	fi
	rm -f "$UNITS/arch-update-cactus-sync.service" "$UNITS/arch-update-cactus-sync.timer"
	systemctl --user daemon-reload 2>/dev/null || true
	for p in "${PROGRAMS[@]}"; do
		[[ -L $BIN/$p ]] && rm -f "$BIN/$p"
	done
	rm -f "$APPS/arch-update.desktop" "$HOME/.local/share/konsole/ArchUpdate.profile" \
		"$HOME/.local/share/konsole/ArchUpdate.colorscheme"
	say "удалено. Сам arch-update, его значок в трее, журналы ($STATE) и папка $DEST остались."
	say "Папку можно убрать вручную: rm -rf $DEST"
}

main() {
	[[ -n $SRC && -f $SRC ]] && ROOT=$(cd "$(dirname "$(realpath "$SRC")")" && pwd)
	if [[ -z ${1-} && $ROOT != "$DEST_REAL" ]]; then
		bootstrap "$@"
	fi
	[[ -n $ROOT ]] || die "${1-}: запускай этот режим из папки апдейтера: $DEST/install.sh ${1-}"
	case ${1-} in
	--sync)
		# only the installed copy: from a development clone it would point the machine at that clone
		[[ $ROOT == "$DEST_REAL" ]] || die "--sync только для установленной копии: $DEST/install.sh --sync"
		install_files
		;;
	--check) self_check ;;
	--uninstall) uninstall ;;
	"")
		[[ $EUID -ne 0 ]] || die "запусти от своего пользователя, не от root"
		install_packages
		self_check || die "самопроверка не прошла — установка остановлена"
		install_files
		git -C "$ROOT" rev-parse HEAD >"$STATE/sync.ok"     # this version passed here
		setup_arch_update
		report
		;;
	*) die "неизвестный параметр: $1 (есть --sync, --check, --uninstall)" ;;
	esac
}

# Last line on purpose: a download cut short by the network never runs half an installer.
main "$@"
