#!/usr/bin/env bash
# Fill tiles/ (next to this script) with the GLB files this export needs, taken from the tile
# cache of your Earth Tile Library project. The list of tiles is in tiles.txt.
#
#   ./fill-tiles.sh [--link] <project folder>
#
#   <project folder>  the earth-rea folder, e.g. ~/Downloads/earth-rea
#                     (or the cache folder itself: .../webapp/data/library/__CACHE_NAME__)
#   --link            create symlinks into the cache instead of copies. Instant and uses no
#                     space, but the export then only works on this computer while the cache
#                     still has the tiles.
#
# Copies use copy-on-write clones where the disk supports it (APFS on macOS, Btrfs/XFS on
# Linux): instant and no extra space until a file is changed. Otherwise files are copied.
# Tiles already in tiles/ are skipped, so running it again is safe.

set -eu

CACHE_NAME="__CACHE_NAME__"

usage() {
	echo "Usage: $0 [--link] <project folder>   (e.g. $0 ~/Downloads/earth-rea)" >&2
	exit 2
}

mode=copy
project=""
for arg in "$@"; do
	case "$arg" in
		--link | -l) mode=link ;;
		-h | --help) usage ;;
		-*) echo "Unknown option: $arg" >&2; usage ;;
		*) [ -z "$project" ] || usage; project="$arg" ;;
	esac
done
[ -n "$project" ] || usage

here="$(cd "$(dirname "$0")" && pwd)"
list="$here/tiles.txt"
out="$here/tiles"
[ -f "$list" ] || { echo "tiles.txt is missing next to this script." >&2; exit 1; }

# Find the cache folder.
src=""
for c in "$project/webapp/data/library/$CACHE_NAME" "$project/library/$CACHE_NAME" "$project/$CACHE_NAME" "$project"; do
	if [ -d "$c" ] && [ "$(basename "$(cd "$c" && pwd)")" = "$CACHE_NAME" ]; then
		src="$(cd "$c" && pwd)"
		break
	fi
done
if [ -z "$src" ]; then
	echo "No tile cache ($CACHE_NAME) found in: $project" >&2
	echo "Pass the earth-rea project folder, e.g. ~/Downloads/earth-rea" >&2
	exit 1
fi

total=$(grep -c . "$list" || true)
echo "Tile cache: $src"
echo "Tiles needed: $total"

# Check everything first, so nothing is half done when tiles are missing.
missing=0
todo="$here/.fill-tiles-todo.$$"
trap 'rm -f "$todo"' EXIT
: > "$todo"
mkdir -p "$out"
while IFS= read -r p || [ -n "$p" ]; do
	[ -n "$p" ] || continue
	if [ ! -f "$src/$p.glb" ]; then
		[ "$missing" -lt 20 ] && echo "  missing: $p.glb" >&2
		missing=$((missing + 1))
	elif [ ! -e "$out/$p.glb" ] && [ ! -L "$out/$p.glb" ]; then
		echo "$src/$p.glb" >> "$todo"
	fi
done < "$list"
if [ "$missing" -gt 0 ]; then
	echo "$missing tile(s) are not in the cache (deleted from the library, or the cache was cleared)." >&2
	echo "Export the area again in the web interface to convert them, then run this script again." >&2
	exit 1
fi

n=$(grep -c . "$todo" || true)
skipped=$((total - n))
if [ "$n" -eq 0 ]; then
	echo "All $total tiles are already in tiles/."
	exit 0
fi

# Pick the fastest way to copy that works here.
first="$(head -n 1 "$todo")"
probe="$out/.fill-tiles-probe.$$"
if [ "$mode" = link ]; then
	how="symlinks"
	cmd="ln -s"
elif cp -c "$first" "$probe" 2>/dev/null; then
	how="clones (APFS)"
	cmd="cp -c"
elif cp --reflink=auto "$first" "$probe" 2>/dev/null; then
	how="copies (clones where the disk supports them)"
	cmd="cp --reflink=auto"
else
	how="copies"
	cmd="cp"
fi
rm -f "$probe"

echo "Creating $n $how in $out ..."
# In batches: one cp/ln call for many files is much faster than one call per file.
done_n=0
batch="$here/.fill-tiles-batch.$$"
trap 'rm -f "$todo" "$batch"' EXIT
while :; do
	tail -n +$((done_n + 1)) "$todo" | head -n 500 > "$batch"
	k=$(grep -c . "$batch" || true)
	[ "$k" -gt 0 ] || break
	# shellcheck disable=SC2086
	tr '\n' '\0' < "$batch" | xargs -0 sh -c "$cmd \"\$@\" \"\$0\"" "$out/"
	done_n=$((done_n + k))
	printf '\r  %d / %d' "$done_n" "$n"
done
echo

have=$(find "$out" -name '*.glb' | wc -l | tr -d ' ')
echo "Done: $n new, $skipped already there; tiles/ now has $have of $total tiles."
[ "$have" -ge "$total" ] || { echo "Some tiles are still missing in tiles/." >&2; exit 1; }
