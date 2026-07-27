#!/usr/bin/env sh
set -eu

platform=''
destination=''
repository='qingfengyugui/invoice-layout-agent'
force=0
skip_mcp=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --platform) platform=$2; shift 2 ;;
        --destination) destination=$2; shift 2 ;;
        --repository) repository=$2; shift 2 ;;
        --force) force=1; shift ;;
        --skip-mcp) skip_mcp=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$platform" in
    codex|claude-code|openclaw|workbuddy|qoder|qclaw) ;;
    *) echo 'Use --platform codex|claude-code|openclaw|workbuddy|qoder|qclaw' >&2; exit 2 ;;
esac
system=$(uname -s)
machine=$(uname -m)
case "$system:$machine" in
    Linux:x86_64|Linux:amd64) target='linux-x64' ;;
    Darwin:arm64|Darwin:aarch64) target='macos-arm64' ;;
    Darwin:x86_64|Darwin:amd64) target='macos-x64' ;;
    *) echo "No complete runtime bundle for $system $machine" >&2; exit 2 ;;
esac

user_profile=${HOME:?User profile is unavailable}
case "$platform" in
    codex) default_destination="$user_profile/.agents/skills" ;;
    claude-code|workbuddy) default_destination="$user_profile/.claude/skills" ;;
    openclaw) default_destination="$user_profile/.openclaw/skills" ;;
    qoder) default_destination="$user_profile/.qoder/skills" ;;
    qclaw) default_destination="$user_profile/.qclaw/skills" ;;
esac
if [ -z "$destination" ]; then destination=$default_destination; fi

asset_name="invoice-layout-agent-$target.tar.gz"
release_base="https://github.com/$repository/releases/latest/download"
task_install_root=$(mktemp -d "${TMPDIR:-/tmp}/invoice-layout-install.XXXXXX")
trap 'rm -rf "$task_install_root"' EXIT HUP INT TERM
curl -fsSL "$release_base/$asset_name" -o "$task_install_root/$asset_name"
curl -fsSL "$release_base/SHA256SUMS" -o "$task_install_root/SHA256SUMS"
expected=$(awk -v name="$asset_name" '$2 == name || $2 == "*" name {print $1; exit}' "$task_install_root/SHA256SUMS")
if [ -z "$expected" ]; then echo "Checksum entry missing for $asset_name" >&2; exit 1; fi
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$task_install_root/$asset_name" | awk '{print $1}')
else
    actual=$(shasum -a 256 "$task_install_root/$asset_name" | awk '{print $1}')
fi
if [ "$actual" != "$expected" ]; then echo 'Runtime bundle checksum mismatch.' >&2; exit 1; fi

mkdir "$task_install_root/expanded"
tar -xzf "$task_install_root/$asset_name" -C "$task_install_root/expanded"
install_base="$user_profile/.invoice-layout-agent"
runtime_root="$install_base/current"
mkdir -p "$install_base"
if [ -e "$runtime_root" ]; then
    if [ "$force" -ne 1 ]; then echo "Runtime already exists: $runtime_root. Re-run with --force to upgrade." >&2; exit 1; fi
    mv "$runtime_root" "$install_base/backup-$(date +%Y%m%d-%H%M%S)"
fi
mv "$task_install_root/expanded" "$runtime_root"

shim_root="$user_profile/.local/bin"
mkdir -p "$shim_root"
executable="$runtime_root/invoice-layout"
shim="$shim_root/invoice-layout"
printf '#!/usr/bin/env sh\nexec "%s" "$@"\n' "$executable" > "$shim"
chmod 755 "$shim" "$executable"

skill_destination="$destination/invoice-layout-agent"
mkdir -p "$destination"
if [ -e "$skill_destination" ]; then
    if [ "$force" -ne 1 ]; then echo "Skill already exists: $skill_destination. Re-run with --force to upgrade." >&2; exit 1; fi
    mv "$skill_destination" "$skill_destination.backup-$(date +%Y%m%d-%H%M%S)"
fi
cp -R "$runtime_root/platforms/$platform/invoice-layout-agent" "$skill_destination"
printf 'Use this complete runtime executable for every command:\n\n`%s`\n' "$executable" > "$skill_destination/RUNTIME.md"

if [ "$skip_mcp" -ne 1 ]; then
    if [ "$platform" = codex ] && command -v codex >/dev/null 2>&1; then
        codex mcp add invoice-layout -- "$executable" mcp || true
    elif { [ "$platform" = claude-code ] || [ "$platform" = workbuddy ]; } && command -v claude >/dev/null 2>&1; then
        claude mcp add --transport stdio invoice-layout -- "$executable" mcp || true
    elif [ "$platform" = qoder ] && command -v qodercli >/dev/null 2>&1; then
        qodercli mcp add invoice-layout -- "$executable" mcp || true
    fi
fi

"$executable" doctor
printf 'Installed complete runtime: %s\nInstalled Skill: %s\n' "$runtime_root" "$skill_destination"
printf '%s\n' 'No Python, Java, WPS, Poppler, OCR, Maven, Docker, or archive-tool installation is required.'
