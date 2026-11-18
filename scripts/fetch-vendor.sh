#!/usr/bin/env bash
# Downloads Elvin's front-end libraries from the npm registry ONCE, verifies each
# tarball against the registry's published sha512 integrity hash, and copies only
# the files the app serves into elvin/static/vendor/. Nothing is loaded from a CDN
# at runtime. Re-run only when bumping a version below.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/elvin/static/vendor"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

PKGS=(
  "@fortawesome/fontawesome-free 7.3.1"
  "chart.js 4.6.1"
  "codemirror 5.65.21"
  "@fontsource-variable/inter 5.3.0"
  "@fontsource-variable/jetbrains-mono 5.3.0"
)

fetch() { # name version -> extracted dir
  local name="$1" ver="$2" base tgz meta integ calc dir
  base="${name##*/}"
  meta="$(curl -fsSL "https://registry.npmjs.org/${name/\//%2F}/$ver")"
  tgz="$(printf '%s' "$meta" | sed -n 's/.*"tarball":"\([^"]*\)".*/\1/p')"
  integ="$(printf '%s' "$meta" | sed -n 's/.*"integrity":"sha512-\([^"]*\)".*/\1/p')"
  dir="$WORK/$base-$ver"; mkdir -p "$dir"
  curl -fsSL "$tgz" -o "$dir.tgz"
  calc="$(openssl dgst -sha512 -binary "$dir.tgz" | openssl base64 -A)"
  if [ "$calc" != "$integ" ]; then echo "INTEGRITY MISMATCH for $name@$ver" >&2; exit 1; fi
  tar -xzf "$dir.tgz" -C "$dir"
  echo "$dir/package"
}

rm -rf "$VENDOR"; mkdir -p "$VENDOR"
for spec in "${PKGS[@]}"; do
  set -- $spec; name="$1"; ver="$2"
  src="$(fetch "$name" "$ver")"
  echo "verified $name@$ver"
  case "$name" in
    @fortawesome/fontawesome-free)
      mkdir -p "$VENDOR/fontawesome/css" "$VENDOR/fontawesome/webfonts"
      cp "$src/css/all.min.css" "$VENDOR/fontawesome/css/"
      cp "$src"/webfonts/*.woff2 "$VENDOR/fontawesome/webfonts/"
      cp "$src/LICENSE.txt" "$VENDOR/fontawesome/LICENSE.txt" ;;
    chart.js)
      mkdir -p "$VENDOR/chartjs"; cp "$src/dist/chart.umd.min.js" "$VENDOR/chartjs/"
      cp "$src/LICENSE.md" "$VENDOR/chartjs/LICENSE.md" ;;
    codemirror)
      mkdir -p "$VENDOR/codemirror/lib" "$VENDOR/codemirror/mode" "$VENDOR/codemirror/addon"
      cp "$src/lib/codemirror.js" "$src/lib/codemirror.css" "$VENDOR/codemirror/lib/"
      for m in python javascript css xml htmlmixed markdown shell sql go rust clike yaml dockerfile toml php ruby swift jsx; do
        mkdir -p "$VENDOR/codemirror/mode/$m"; cp "$src/mode/$m/$m.js" "$VENDOR/codemirror/mode/$m/"; done
      for a in edit/closebrackets edit/matchbrackets edit/closetag selection/active-line comment/comment \
               search/searchcursor search/search dialog/dialog mode/simple mode/overlay; do
        mkdir -p "$VENDOR/codemirror/addon/$(dirname $a)"; cp "$src/addon/$a.js" "$VENDOR/codemirror/addon/$a.js"; done
      cp "$src/addon/dialog/dialog.css" "$VENDOR/codemirror/addon/dialog/"
      cp "$src/LICENSE" "$VENDOR/codemirror/LICENSE" ;;
    @fontsource-variable/inter)
      mkdir -p "$VENDOR/fonts"; cp "$src/files/inter-latin-wght-normal.woff2" "$VENDOR/fonts/inter-latin-wght-normal.woff2"
      cp "$src/LICENSE" "$VENDOR/fonts/LICENSE-inter.txt" ;;
    @fontsource-variable/jetbrains-mono)
      mkdir -p "$VENDOR/fonts"; cp "$src/files/jetbrains-mono-latin-wght-normal.woff2" "$VENDOR/fonts/jetbrains-mono-latin-wght-normal.woff2"
      cp "$src/LICENSE" "$VENDOR/fonts/LICENSE-jetbrains-mono.txt" ;;
  esac
done
cat > "$VENDOR/VERSIONS.txt" <<V
Downloaded by scripts/fetch-vendor.sh (sha512-verified against registry.npmjs.org)
$(printf '%s\n' "${PKGS[@]}")
tailwindcss 4.3.4 (built at image-build time, see Dockerfile stage "assets")
V
echo "vendor assets ready in $VENDOR"
