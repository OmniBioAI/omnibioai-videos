#!/bin/sh
# Runtime publisher for the public video portal (container entrypoint).
#
# The host content directory is mounted read-only at $CONTENT_DIR, but mounting a
# directory does not make its files web-accessible: nginx only knows the exact-match
# locations generated below, one per approved file (see scripts/build_public.py).
#
# The image carries, for each PUBLIC video, its approved SHA-256 and size
# (/etc/publication/approved.tsv) plus a catalog record and an nginx location
# fragment. At start-up and then continuously this script checks the mounted
# file against the approved hash and only then publishes it: catalog entry AND
# direct media URL together. A file that is missing, a symlink, resized,
# replaced or modified is withheld -- delisted from /videos.json and 404 on its URL --
# until the approved bytes are back (or the image is rebuilt after re-review).
#
#   PUBLISH_POLL_SECONDS    how often file size/mtime/inode are re-checked (default 5)
#   PUBLISH_REHASH_SECONDS  how often every file is fully re-hashed regardless (default 600)
#
# Runs on busybox sh (nginx:alpine): no python, jq or bash.
set -u
umask 022

PUBLICATION_DIR=${PUBLICATION_DIR:-/etc/publication}
CONTENT_DIR=${CONTENT_DIR:-/content}
GENERATED_DIR=${GENERATED_DIR:-/run/publication}
POLL=${PUBLISH_POLL_SECONDS:-5}
REHASH=${PUBLISH_REHASH_SECONDS:-600}
STATE="$GENERATED_DIR/state"
APPROVED="$PUBLICATION_DIR/approved.tsv"
TAB=$(printf '\t')

log() { echo "publish: $*"; }

file_sig() { stat -c '%s:%y:%i' "$1" 2>/dev/null || echo missing; }

# id sha size name -> success only if the mounted file is exactly the approved bytes
verify_file() {
    f="$CONTENT_DIR/$4"
    [ -f "$f" ] && [ ! -L "$f" ] || return 1
    [ "$(stat -c %s "$f" 2>/dev/null)" = "$3" ] || return 1
    [ "$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)" = "$2" ]
}

# $1=1 forces a full re-hash of every file. Success (0) means the published set changed.
refresh() {
    force=$1
    changed=1
    while IFS="$TAB" read -r id sha size name; do
        [ -n "$id" ] || continue
        sig=$(file_sig "$CONTENT_DIR/$name")
        was_ok=0
        [ -f "$STATE/$id.ok" ] && was_ok=1
        if [ "$force" = 1 ] || [ ! -f "$STATE/$id.sig" ] || [ "$sig" != "$(cat "$STATE/$id.sig")" ]; then
            now_ok=0
            verify_file "$id" "$sha" "$size" "$name" && now_ok=1
            printf '%s' "$sig" > "$STATE/$id.sig"
            if [ "$now_ok" = 1 ]; then : > "$STATE/$id.ok"; else rm -f "$STATE/$id.ok"; fi
            if [ "$now_ok" != "$was_ok" ]; then
                changed=0
                if [ "$now_ok" = 1 ]; then log "$name VERIFIED -> published"; else log "$name NOT the approved bytes -> withheld (delisted, 404)"; fi
            fi
        fi
    done < "$APPROVED"
    return $changed
}

# Write media.conf + videos.json from the currently verified set. $1=1 -> nginx is already running.
apply() {
    running=$1
    : > "$GENERATED_DIR/media.conf.tmp"
    printf '[' > "$GENERATED_DIR/videos.json.tmp"
    sep=''
    while IFS="$TAB" read -r id sha size name; do
        [ -n "$id" ] || continue
        [ -f "$STATE/$id.ok" ] || continue
        cat "$PUBLICATION_DIR/entries/$id.conf" >> "$GENERATED_DIR/media.conf.tmp"
        printf '%s%s' "$sep" "$(cat "$PUBLICATION_DIR/entries/$id.json")" >> "$GENERATED_DIR/videos.json.tmp"
        sep=','
    done < "$APPROVED"
    printf ']\n' >> "$GENERATED_DIR/videos.json.tmp"
    # Access first, listing second: never list something that would 404, and stop serving before delisting.
    mv "$GENERATED_DIR/media.conf.tmp" "$GENERATED_DIR/media.conf"
    if [ "$running" = 1 ]; then
        nginx -t -q || { log "generated nginx config invalid; refusing to continue"; exit 1; }
        nginx -s reload
    fi
    mv "$GENERATED_DIR/videos.json.tmp" "$GENERATED_DIR/videos.json"
}

mkdir -p "$STATE"
[ -r "$APPROVED" ] || { log "missing $APPROVED"; exit 1; }
refresh 1
apply 0
nginx -t -q || { log "nginx configuration test failed"; exit 1; }
log "starting nginx; $(ls "$STATE" | grep -c '\.ok$') of $(grep -c . "$APPROVED") approved video(s) verified"

nginx -g 'daemon off;' &
NGINX_PID=$!
trap 'log stopping; nginx -s quit 2>/dev/null; wait "$NGINX_PID"; exit 0' TERM INT QUIT

last_full=$(date +%s)
while kill -0 "$NGINX_PID" 2>/dev/null; do
    sleep "$POLL" &
    wait $!
    now=$(date +%s)
    force=0
    if [ $((now - last_full)) -ge "$REHASH" ]; then force=1; last_full=$now; fi
    if refresh "$force"; then apply 1; fi
done
log "nginx exited"
exit 1
