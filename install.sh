#!/usr/bin/env bash
# Arch Update by Cactus — installer.
#
# First install on a machine (Arch Linux, as your normal user, not root):
#   curl -fsSL https://raw.githubusercontent.com/Purple-Palm/arch-update-cactus/stable/install.sh | bash
#
#   install.sh             install or repair: packages, files, tray icon, timers (asks before installing;
#                          at a keyboard, once, whether update reports may go to the developer)
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
BANNER_SHOWN=${ARCH_UPDATE_BANNER_SHOWN-}     # the bootstrap showed it before starting this copy
unset ARCH_UPDATE_BANNER_SHOWN

# ── output: the look of the UI on a terminal, plain text in a pipe or a log (sync.log, the tests) ──

WIDTH=80        # lines are broken between words to fit (a narrower terminal: its own width)
TTY=0           # stdout and stderr are a terminal: a "●" line can make way for its result
PENDING=0       # such a line is on the screen now, without its newline
OFF='' BOLD='' ACCENT='' DIM='' MUTED='' GOOD='' WARN='' BAD='' COLOUR=''
# The UI's palette (bin/arch-update-ui); the banner and the card run along its accent gradient
# #1793d1 #4f7bff #8b5cff #d65cff #ff5ca8. Text keeps the terminal's own colour: the UI's TEXT
# (#c9d1e3) is made for the UI's dark background and would all but vanish on a light terminal.
GRAD_R=(23 79 139 214 255) GRAD_G=(147 123 92 92 92) GRAD_B=(209 255 255 255 168)
if [[ -t 1 && -t 2 && ${TERM:-dumb} != dumb ]]; then
	TTY=1
	cols=$(stty size 2>/dev/null </dev/tty) || cols=
	cols=${cols#* }
	if [[ $cols =~ ^[0-9]+$ ]] && ((cols > 0 && cols < WIDTH)); then   # 0: the size is not known
		WIDTH=$((cols > 40 ? cols : 40))
	fi
	if [[ -z ${NO_COLOR-} ]]; then                # https://no-color.org
		OFF=$'\e[0m' BOLD=$'\e[1m'
		ACCENT=$'\e[38;2;79;123;255m'             # #4f7bff
		DIM=$'\e[38;2;107;117;145m'               # #6b7591
		MUTED=$'\e[38;2;78;88;118m'               # #4e5876
		GOOD=$'\e[38;2;61;220;132m'               # #3ddc84
		WARN=$'\e[38;2;255;196;87m'               # #ffc457
		BAD=$'\e[38;2;255;92;122m'                # #ff5c7a
	fi
fi
trap '((PENDING == 0)) || echo' EXIT            # a "●" line cut short still ends before the prompt

# COLOUR = point $1 of $2 along the accent gradient (empty without colours).
gradient() {
	COLOUR=
	[[ -n $OFF ]] || return 0
	local n=$((${#GRAD_R[@]} - 1)) p i f
	p=$(($1 * n * 256 / ($2 > 1 ? $2 - 1 : 1)))
	i=$((p / 256)) f=$((p % 256))
	if ((i >= n)); then
		i=$((n - 1)) f=256
	fi
	printf -v COLOUR '\e[38;2;%d;%d;%dm' $(((GRAD_R[i] * (256 - f) + GRAD_R[i + 1] * f) / 256)) \
		$(((GRAD_G[i] * (256 - f) + GRAD_G[i + 1] * f) / 256)) $(((GRAD_B[i] * (256 - f) + GRAD_B[i + 1] * f) / 256))
}

# "ARCH UPDATE" in half-block letters along the gradient, then "by Cactus"; one line on a terminal
# narrower than 60 columns. The letters are spelled in ASCII (# █, ^ ▀, _ ▄), so that a row can be
# cut into columns in any locale.
banner() {
	local rows=('_^# #^# #^^ # #   # # #^# #^_ _^# ^#^ #^^' '#^# #^_ #__ #^#   #_# #^^ #_^ #^#  #  ##_')
	local row out x ch lines=()
	((WIDTH >= 60)) || rows=('A R C H   U P D A T E')
	for row in "${rows[@]}"; do
		out=
		((${#rows[@]} > 1)) || out=$BOLD
		for ((x = 0; x < ${#row}; x++)); do
			ch=${row:x:1}
			case $ch in
			' ')
				out+=' '
				continue
				;;
			'#') ch=█ ;;
			'^') ch=▀ ;;
			_) ch=▄ ;;
			esac
			gradient "$x" "${#row}"
			out+=$COLOUR$ch
		done
		lines+=("$out$OFF")
	done
	echo
	for ((x = 0; x < ${#lines[@]} - 1; x++)); do
		printf '  %s\n' "${lines[x]}"
	done
	printf '  %s  %sby Cactus%s\n\n' "${lines[-1]}" "$DIM" "$OFF"
}

# WRAPPED = $2 broken between words into lines of at most WIDTH - $1 columns. A $2 that starts with
# spaces is a command and stays one line, indented by them: copied from the screen, it has to run as
# it is (a terminal narrower than the command folds it, and a copy still gives one line). (${#}
# counts letters in a UTF-8 locale and bytes in "C": there a line comes out shorter, never longer.)
WRAPPED=()
wrap() {
	local room=$((WIDTH - $1)) word line=''
	local -a words=()
	WRAPPED=()
	if [[ $2 == ' '* ]]; then
		WRAPPED=("$2")
		return 0
	fi
	read -r -d '' -a words <<<"$2" || true
	for word in "${words[@]}"; do
		if [[ -n $line ]] && ((${#line} + 1 + ${#word} > room)); then
			WRAPPED+=("$line")
			line=$word
		else
			line=${line:+$line }$word
		fi
	done
	WRAPPED+=("$line")
}

# End a "●" line that waits for its result: its ✓, ! or ✗ line takes its place.
settle() {
	((PENDING)) || return 0
	printf '\r\e[2K'
	PENDING=0
}

# "  <mark> <text>": the mark in its colour, the text broken to fit; each further argument starts a
# line of its own under the text. No mark: the lines only, indented as under one.
mark() {
	local sign=$1 colour=$2 part i
	shift 2
	settle
	for part in "$@"; do
		wrap 4 "$part"
		for i in "${!WRAPPED[@]}"; do
			if [[ -n $sign ]]; then
				printf '  %s%s%s %s\n' "$colour" "$sign" "$OFF" "${WRAPPED[i]}"
				sign=
			else
				printf '    %s\n' "${WRAPPED[i]}"
			fi
		done
	done
}

step() { mark '●' "$ACCENT" "$@"; }        # a step whose own output (pacman, makepkg, yay) follows
warn() { mark '!' "$WARN" "$@" >&2; }
die() {
	mark '✗' "$BAD" "$@" >&2
	exit 1
}

# "  ✓ <step> · <detail>": a step done, or a check that found everything in place.
ok() {
	local i
	settle
	wrap 4 "$1${2:+ · $2}"
	for i in "${!WRAPPED[@]}"; do
		if ((i == 0)); then
			printf '  %s✓%s %s%s%s%s\n' "$GOOD" "$OFF" "$1" "$DIM" "${WRAPPED[0]#"$1"}" "$OFF"
		else
			printf '    %s%s%s\n' "$DIM" "${WRAPPED[i]}" "$OFF"
		fi
	done
}

# A step that takes a moment and writes nothing itself: "  ● <step>…" until its result takes the
# line (only on a terminal; elsewhere the result is enough).
pending() {
	((TTY)) || return 0
	settle
	printf '  %s●%s %s…' "$ACCENT" "$OFF" "$1"
	PENDING=1
}

# An aligned block (the card about reports, the final report) is collected in BLOCK first. A row is
# a label of 10 columns, padded where it is written (printf would pad bytes, not letters), and its
# value broken to fit beside it; each further argument continues the value on a line of its own. A
# part that does not fit there (a long path, or a command, on a narrow terminal) goes under the
# label instead, two columns in, or none for a line that needs them. A command starts with spaces,
# as for wrap(); here they only mark it and are not shown.
BLOCK=()
row() {
	local label=$1 part line under
	shift
	for part in "$@"; do
		wrap 16 "$part"
		under=0
		for line in "${WRAPPED[@]}"; do
			line=${line#"${line%%[! ]*}"}
			((${#line} <= WIDTH - 16)) || under=1
		done
		if ((under)); then
			wrap 6 "$part"
			[[ -z $label ]] || BLOCK+=("$DIM${label%"${label##*[! ]}"}$OFF")
			label=
		fi
		for line in "${WRAPPED[@]}"; do
			line=${line#"${line%%[! ]*}"}
			if [[ -n $label ]]; then
				BLOCK+=("$DIM$label$OFF  $line")
				label=
			elif ((!under)); then
				BLOCK+=("            $line")
			elif ((${#line} <= WIDTH - 6)); then
				BLOCK+=("  $line")
			else
				BLOCK+=("$line")
			fi
		done
	done
}

# Print BLOCK and empty it: indented four columns, or as a card, the gradient down its left edge.
block() {
	local i
	for i in "${!BLOCK[@]}"; do
		if [[ ${1-} == card ]]; then
			gradient "$i" "${#BLOCK[@]}"
			printf '  %s┃%s%s\n' "$COLOUR" "$OFF" "${BLOCK[i]:+ ${BLOCK[i]}}"
		else
			printf '    %s\n' "${BLOCK[i]}"
		fi
	done
	BLOCK=()
}

# Packages are installed through sudo; a fresh Arch install may not have it yet.
need_sudo() {
	command -v sudo >/dev/null && return 0
	die "нет sudo, а пакеты ставятся через него. Один раз под root (su -):" "  pacman -S sudo" \
		"  usermod -aG wheel $(id -un)" "  echo '%wheel ALL=(ALL:ALL) ALL' >/etc/sudoers.d/wheel" \
		"Потом перелогинься и запусти установщик снова"
}

# Where this script lives; empty when it is piped from curl.
SRC=${BASH_SOURCE[0]-}
ROOT=

# ── bootstrap: piped from curl, or started from some other copy → work from the real checkout ──

bootstrap() {
	local err old
	[[ $EUID -ne 0 ]] || die "запусти от своего пользователя, не от root (sudo спросится сам, когда понадобится)"
	command -v pacman >/dev/null || die "это не Arch Linux: нет pacman"
	[[ ! -L $DEST ]] || die "$DEST — ссылка (на копию для разработки?), её не трогаю"
	if ! command -v git >/dev/null; then
		step "ставлю git: через него апдейтер скачивается и обновляется"
		need_sudo
		{ sudo pacman -S --needed git; } </dev/tty
		ok "git" "поставлен"
	fi
	if [[ -d $DEST/.git ]]; then
		pending "получаю последнюю версию"
		old=$(git -C "$DEST" rev-parse HEAD 2>/dev/null) || old=
		err=$(git -C "$DEST" fetch --quiet origin "$TAG" 2>&1 && git -C "$DEST" reset --quiet --hard FETCH_HEAD 2>&1) ||
			die "не удалось обновить ${DEST/#"$HOME"/\~} с GitHub:" "$err"
		if [[ $(git -C "$DEST" rev-parse HEAD) == "$old" ]]; then
			ok "апдейтер" "последняя версия уже стоит"
		else
			ok "апдейтер" "обновлён до последней версии"
		fi
	else
		pending "скачиваю Arch Update by Cactus"
		err=$(git -c advice.detachedHead=false clone --quiet --branch "$TAG" "https://github.com/$REPO.git" "$DEST" 2>&1) ||
			die "не удалось скачать апдейтер в ${DEST/#"$HOME"/\~}:" "$err"
		ok "апдейтер" "скачан в ${DEST/#"$HOME"/\~}"
	fi
	export ARCH_UPDATE_BANNER_SHOWN=1
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
	# coreutils only: cmp comes with diffutils, which a plain Arch install does not have
	if [[ -f $dst && $(sha256sum <"$tmp") == "$(sha256sum <"$dst")" ]]; then
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
	warn "старый ~/$rel сохранён в ${BACKUP/#"$HOME"/\~}/"
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
		"$ROOT/lib/games.py" "$ROOT/lib/report.py" "$ROOT"/lib/fixes/*.py || return 1
	# the fixes' root shell (lib/fixes) is bash inside Python: it has to parse too
	python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import fixes; print(fixes.runner())' "$ROOT/lib" \
		| bash -n || return 1
	python3 "$ROOT/bin/arch-update-ui" --self-test || return 1
}

# Build and install one AUR package with plain makepkg (asks for sudo itself).
makepkg_aur() {
	local tmp ok=0
	tmp=$(mktemp -d)
	git clone --quiet "https://aur.archlinux.org/$1.git" "$tmp/$1" && (cd "$tmp/$1" && makepkg -si --needed) && ok=1
	rm -rf "$tmp"
	((ok))
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
	local again="Исправь и запусти установщик снова: что уже стоит, второй раз не ставится."
	if ((${#missing[@]})); then
		step "ставлю пакеты: ${missing[*]}"
		need_sudo
		sudo pacman -S --needed "${missing[@]}" ||
			die "пакеты не поставились." "Если не пустил sudo — проверь: sudo -v" \
				"Если pacman не нашёл их на зеркале — сначала обнови систему:" "  sudo pacman -Syu" "$again"
		ok "пакеты" "поставлены"
	else
		ok "пакеты" "всё нужное уже стоит"
	fi
	# AUR packages: yay, as on the main machine (the auto-answers know its questions best)
	local helper
	if helper=$(aur_helper); then
		ok "AUR" "через $helper"
	else
		step "ставлю yay — помощник для пакетов из AUR"
		need_sudo
		sudo pacman -S --needed base-devel || die "не поставился base-devel (нужен, чтобы собрать yay)." "$again"
		makepkg_aur yay-bin || die "yay не собрался: AUR недоступен или сборка упала (её вывод выше)." "$again"
		helper=yay
		ok "AUR" "yay поставлен"
	fi
	if command -v arch-update >/dev/null; then
		ok "arch-update" "уже стоит"
		return 0
	fi
	local pkg=arch-update
	[[ $(uname -m) == x86_64 ]] && pkg=arch-update-bin      # prebuilt; elsewhere it is built from source
	step "ставлю $pkg из AUR через $helper"
	need_sudo
	"$helper" -S --needed "$pkg" || die "$pkg не поставился из AUR (вывод $helper выше)." "$again"
	command -v arch-update >/dev/null || die "arch-update не установился"
	ok "arch-update" "поставлен"
}

setup_arch_update() {
	local config=${XDG_CONFIG_HOME:-$HOME/.config} helper   # where arch-update itself keeps its files
	local conf=$config/arch-update/arch-update.conf
	if [[ ! -f $conf ]]; then
		arch-update --gen-config >/dev/null
		# As on the main machine: two old versions stay in the package cache; yay for the AUR.
		sed -i -e 's/^#KeepOldPackages=.*/KeepOldPackages=2/' "$conf"
		if helper=$(aur_helper); then
			sed -i "s/^#AURHelper=.*/AURHelper=$helper/" "$conf"
		fi
	fi
	if grep -qE '^[[:space:]]*NoVersion' "$conf"; then
		warn "в ${conf/#"$HOME"/\~} включён NoVersion: без версий апдейтер не увидит список обновлений." \
			"Закомментируй его"
	fi
	local autostart=$config/autostart/arch-update-tray.desktop
	[[ -f $autostart ]] || arch-update --tray --enable >/dev/null || true
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
	# Claim only what is so. The icon is there when the applet runs in a graphical session (one just
	# started gets two seconds to come up); from ssh or a console it has no screen to show on here,
	# and its autostart entry brings it at the next login.
	local screen=${WAYLAND_DISPLAY-}${DISPLAY-} up=0 i
	if [[ -n $screen ]]; then
		for ((i = 0; i < 10; i++)); do
			if pgrep -xu "$USER" arch-update-tra >/dev/null; then
				up=1
				break
			fi
			sleep 0.2
		done
	fi
	if ((up)); then
		ok "arch-update" "настроен, значок в трее"
	elif [[ -f $autostart && -z $screen ]]; then
		ok "arch-update" "настроен, значок появится при входе"
	else
		ok "arch-update" "настроен"
	fi
	if [[ ! -f $autostart ]]; then
		warn "автозапуск значка в трее не включился:" "  arch-update --tray --enable"
	elif ((!up)) && [[ -n $screen ]]; then
		warn "значок в трее не запустился:" "  arch-update --tray"
	fi
}

# Update reports (lib/report.py) stay off until the user says yes. The question comes once, on an
# install at a keyboard: --sync and --check never ask, and without a terminal the choice stays open
# (Arch-Update → Настройки). Only y/yes/д/да say yes: "н" would be the start of "нет".
ask_reports() {
	local answer='' choice=no REPLY
	[[ -t 0 ]] || return 0
	python3 "$ROOT/lib/report.py" --decided && return 0
	settle
	echo
	BLOCK=("${BOLD}Отчёты об обновлениях$OFF")
	wrap 4 "После каждого обновления Arch Update может отправлять разработчику отчёт:"
	BLOCK+=("${WRAPPED[@]}")
	wrap 4 "так разработчик видит проблему на любой машине, не прося прислать журнал."
	BLOCK+=("${WRAPPED[@]}" "")
	row 'в отчёте  ' "модель устройства и железо, как подключена сеть (Wi-Fi или кабель, без её названия), какие пакеты обновились, итог проверки, журнал обновления"
	row 'не уходят ' "имя пользователя и компьютера, пароли, IP- и MAC-адреса, почта, название Wi-Fi"
	row 'куда      ' "в закрытый репозиторий разработчика на GitHub, через сервер для отчётов на Cloudflare (он видит IP-адрес, как любой сайт, но не хранит его)"
	row 'передумать' "в любой момент: Arch-Update → Настройки"
	block card
	echo
	read -r -s -t 0.1 -N 100000 || true      # an Enter pressed while pacman worked is no answer
	printf '  %s?%s %sОтправлять отчёты?%s %s[y/N]%s ' "$ACCENT" "$OFF" "$BOLD" "$OFF" "$DIM" "$OFF"
	read -r answer || echo
	case ${answer,,} in
	y | yes | д | да | Д | Да | ДА | дА) choice=yes ;;
	esac
	if ! python3 "$ROOT/lib/report.py" --consent "$choice"; then
		warn "ответ не сохранился — выбрать можно в Arch-Update → Настройки"
	elif [[ $choice == yes ]]; then
		ok "отчёты" "включены — спасибо!"
	else
		ok "отчёты" "выключены"
	fi
}

summary() {
	local term de=${XDG_CURRENT_DESKTOP:-?} shown timer=0 reports version i
	term=$("$ROOT/bin/arch-update-launcher" --terminal 2>/dev/null || echo "не найден")
	reports=$(python3 "$ROOT/lib/report.py" --status 2>/dev/null) || reports=выключены
	systemctl --user is-enabled --quiet arch-update-cactus-sync.timer 2>/dev/null && timer=1
	version=$(git -C "$ROOT" log -1 --format='%h от %cd' --date=format:%d.%m.%Y)
	shown=$de
	[[ $de != "?" ]] || shown="не определено"
	settle
	echo
	wrap 4 "Готово — Arch Update by Cactus $version"         # the version wraps on a narrow terminal
	for i in "${!WRAPPED[@]}"; do
		if ((i == 0)); then
			printf '  %s✓%s %sГотово%s — Arch Update by Cactus%s%s%s\n' "$GOOD" "$OFF" "$BOLD" "$OFF" "$MUTED" \
				"${WRAPPED[0]#"Готово — Arch Update by Cactus"}" "$OFF"
		else
			printf '    %s%s%s\n' "$MUTED" "${WRAPPED[i]}" "$OFF"
		fi
	done
	echo
	row 'окружение ' "$shown · терминал: $term · AUR: $(aur_helper || echo нет)"
	row 'запуск    ' "значок Arch-Update в трее или меню приложений"
	row 'демо      ' "  ~/.local/bin/arch-update-launcher --demo"
	if ((timer)); then
		row 'обновления' "новые версии приезжают сами: раз в час, при входе и при запуске" \
			"проверить сейчас: ~/.local/bin/arch-update-cactus-sync"
	else
		row 'обновления' "новые версии приезжают при запуске" "проверить сейчас: ~/.local/bin/arch-update-cactus-sync"
	fi
	row 'отчёты    ' "$reports — переключить: Arch-Update → Настройки"
	block
	echo
	if ((!timer)); then
		warn "фоновые проверки не включились: в этом сеансе нет systemd --user" "(установщик запущен через su?)." \
			"Запусти его ещё раз из терминала на рабочем столе — включатся"
	fi
	if [[ $de == *GNOME* ]] && ! pacman -Q gnome-shell-extension-appindicator >/dev/null 2>&1; then
		warn "в GNOME значок в трее появится только с расширением AppIndicator:" \
			"  sudo pacman -S gnome-shell-extension-appindicator" "затем перелогинься и включи его в «Расширениях»"
	fi
	if [[ $de == "?" ]]; then
		warn "окружение без автозапуска? Добавь в автозапуск своего WM:" "  sleep 3 && arch-update --tray"
	fi
}

uninstall() {
	local p state=${STATE/#"$HOME"/\~} dest=${DEST/#"$HOME"/\~}
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
	ok "удалено" "программы, пункт меню, профиль Konsole, фоновая проверка"
	mark '' '' "Остались сам arch-update и его значок в трее, журналы и настройки ($state) и папка $dest." \
		"Её можно убрать вручную:" "  rm -rf $dest"
}

main() {
	[[ -n $SRC && -f $SRC ]] && ROOT=$(cd "$(dirname "$(realpath "$SRC")")" && pwd)
	if [[ -z ${1-} && -z $BANNER_SHOWN ]]; then
		banner
	fi
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
		pending "самопроверка"
		local out
		if ! out=$(self_check 2>&1); then
			settle
			printf '%s\n' "$out" >&2
			die "самопроверка не прошла — установка остановлена"
		fi
		ok "самопроверка" "пройдена"
		install_files
		ok "файлы" "программы в ~/.local/bin, пункт в меню приложений"
		mkdir -p "$STATE"                                   # a clean machine has no state yet
		git -C "$ROOT" rev-parse HEAD >"$STATE/sync.ok"     # this version passed here
		setup_arch_update
		ask_reports
		summary
		;;
	*) die "неизвестный параметр: $1 (есть --sync, --check, --uninstall)" ;;
	esac
}

# Last line on purpose: a download cut short by the network never runs half an installer.
main "$@"
