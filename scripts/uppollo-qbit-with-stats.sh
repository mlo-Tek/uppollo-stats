#!/bin/bash
set -u
umask 002

###############################################################################
# qBittorrent -> Hardlink -> upPollo + upPollo Stats
#
# qBittorrent Completion Hook:
# /config/scripts/uppollo-qbit.sh "%N" "%F" "%D" "%L" "%G" "%I" "%T"
###############################################################################

# qBittorrent WebAPI
QBIT_URL="http://10.20.20.15:8080"
QBIT_API_KEY="qbt_DEIN_API_KEY"

# upPollo
UPPOLLO_URL="http://DEINE_UPPOLLO_IP:9999/api/upload"
UPPOLLO_API_KEY="DEIN_UPPOLLO_API_KEY"

# Stats Dashboard
STATS_URL="http://DEINE_UNRAID_IP:8781"
STATS_API_KEY="DEIN_STATS_API_KEY"

# Zieltracker nie re-uploaden
TARGET_TRACKER_DOMAIN="rocket-hd.cc"

# Hardlink-Ziel / upPollo Quelle
UPLOAD_ROOT="/data/torrents/uploads"
UPLOAD_TAG="upload-rhd"

# Schutz gegen Doppelverarbeitung
STATE_ROOT="/config/uppollo-qbit-state"
LOG_FILE="/config/logs/uppollo-qbit.log"

ALLOWED_CATEGORIES=(
    "movies"
    "movies-kids"
    "stand-up-comedy"
    "tv"
    "tv-kids"
    "anime"
)

BANNED_GROUPS=(
    "1XBET"
    "MEGA"
    "Whistler"
    "WOTT"
    "HELD"
    "FSX"
    "FuN"
    "w00t"
    "BB"
    "266ers"
    "JellyfinPlex"
    "kellerratte"
    "2BA"
    "FritzBox"
    "FUNXDTV"
    "MTZ"
    "Taylor.D"
    "MagicX"
    "PaTroL"
    "PaZ"
    "RARBG"
    "GTF"
    "PiKACHU"
)

# qBit Arguments
TORRENT_NAME="${1:-}"
CONTENT_PATH="${2:-}"
SAVE_PATH="${3:-}"
CATEGORY="${4:-}"
TAGS="${5:-}"
TORRENT_HASH="${6:-}"
CURRENT_TRACKER="${7:-}"

RELEASE_GROUP=""
TARGET_PATH=""
LOCK_DIR=""

mkdir -p "$(dirname "$LOG_FILE")" "$STATE_ROOT" "$UPLOAD_ROOT"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# Dashboard darf niemals den eigentlichen Upload blockieren.
stats_event() {
    local status="$1"
    local reason="${2:-}"
    local upload_path="${3:-${TARGET_PATH:-}}"

    [[ -n "$STATS_URL" && -n "$STATS_API_KEY" ]] || return 0

    curl -fsS --connect-timeout 0.5 --max-time 1 \
      -H "X-Stats-Key: $STATS_API_KEY" \
      -H "Content-Type: application/json" \
      -X POST "$STATS_URL/api/event" \
      --data "$(jq -nc \
        --arg hash "$TORRENT_HASH" \
        --arg release "$TORRENT_NAME" \
        --arg category "$CATEGORY" \
        --arg group "$RELEASE_GROUP" \
        --arg tracker "$CURRENT_TRACKER" \
        --arg source "$CONTENT_PATH" \
        --arg upload "$upload_path" \
        --arg status "$status" \
        --arg reason "$reason" \
        '{torrent_hash:$hash,release_name:$release,category:$category,release_group:$group,tracker:$tracker,source_path:$source,upload_path:$upload,status:$status,reason:$reason,source:"qbit"}')" \
      >/dev/null 2>&1 || true
}

cleanup() {
    [[ -n "${LOCK_DIR:-}" ]] && rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

log "============================================================"
log "qBittorrent Completion Event"
log "Torrent:         $TORRENT_NAME"
log "Content Path:    $CONTENT_PATH"
log "Save Path:       $SAVE_PATH"
log "Category:        $CATEGORY"
log "Tags:            $TAGS"
log "Hash:            $TORRENT_HASH"
log "Current Tracker: $CURRENT_TRACKER"

# Grundvalidierung
if [[ -z "$TORRENT_NAME" ]]; then log "FEHLER: Torrentname fehlt."; exit 1; fi
if [[ -z "$CONTENT_PATH" ]]; then log "FEHLER: Content Path fehlt."; exit 1; fi
if [[ ! -e "$CONTENT_PATH" ]]; then log "FEHLER: Content Path existiert nicht: $CONTENT_PATH"; exit 1; fi
if [[ -z "$TORRENT_HASH" || "$TORRENT_HASH" == "-" ]]; then log "FEHLER: Kein gültiger Torrent-Hash."; exit 1; fi

stats_event "triggered"

# Nie die neu erzeugten Upload-Torrents erneut verarbeiten.
case "$CONTENT_PATH" in
    "$UPLOAD_ROOT"|"$UPLOAD_ROOT"/*)
        stats_event "skipped_existing" "Quelle liegt bereits unter $UPLOAD_ROOT"
        log "SKIP: Torrent liegt bereits unter $UPLOAD_ROOT"
        exit 0
        ;;
esac

# Kategorie
CATEGORY_ALLOWED=false
for ALLOWED in "${ALLOWED_CATEGORIES[@]}"; do
    [[ "$CATEGORY" == "$ALLOWED" ]] && CATEGORY_ALLOWED=true && break
done
if [[ "$CATEGORY_ALLOWED" != "true" ]]; then
    stats_event "skipped_category" "Kategorie nicht freigegeben: $CATEGORY"
    log "SKIP: Kategorie nicht erlaubt: $CATEGORY"
    exit 0
fi

# upload-rhd bereits vorhanden -> nicht erneut verarbeiten
IFS=',' read -r -a TAG_ARRAY <<< "$TAGS"
for CURRENT_TAG_ITEM in "${TAG_ARRAY[@]}"; do
    CURRENT_TAG_ITEM="$(printf '%s' "$CURRENT_TAG_ITEM" | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//')"
    if [[ "${CURRENT_TAG_ITEM,,}" == "${UPLOAD_TAG,,}" ]]; then
        stats_event "skipped_tag" "Torrent besitzt bereits Tag $UPLOAD_TAG"
        log "SKIP: Torrent besitzt bereits Tag $UPLOAD_TAG"
        exit 0
    fi
done

# State / Lock
STATE_FILE="$STATE_ROOT/${TORRENT_HASH}.submitted"
LOCK_DIR="$STATE_ROOT/${TORRENT_HASH}.lock"
if [[ -f "$STATE_FILE" ]]; then
    stats_event "skipped_existing" "Torrent-Hash wurde bereits verarbeitet"
    log "SKIP: Torrent-Hash wurde bereits verarbeitet."
    exit 0
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: Torrent wird bereits verarbeitet."
    exit 0
fi

# qBit API testen
if ! curl -fsS -H "Authorization: Bearer $QBIT_API_KEY" "$QBIT_URL/api/v2/app/version" >/dev/null; then
    stats_event "failed" "qBittorrent WebAPI nicht erreichbar oder API-Key ungültig"
    log "FEHLER: qBittorrent WebAPI/API-Key."
    exit 1
fi

# Alle Tracker lesen
TRACKERS_JSON="$(curl -fsS -H "Authorization: Bearer $QBIT_API_KEY" --get --data-urlencode "hash=$TORRENT_HASH" "$QBIT_URL/api/v2/torrents/trackers")" || {
    stats_event "failed" "Tracker konnten nicht aus qBittorrent gelesen werden"
    log "FEHLER: Tracker konnten nicht gelesen werden."
    exit 1
}

TARGET_TRACKER_FOUND="$(printf '%s' "$TRACKERS_JSON" | jq -r '.[].url // empty' | grep -Fi -- "$TARGET_TRACKER_DOMAIN" | head -n1 || true)"
if [[ -n "$TARGET_TRACKER_FOUND" ]]; then
    CURRENT_TRACKER="$TARGET_TRACKER_FOUND"
    stats_event "skipped_tracker" "Zieltracker bereits enthalten: $TARGET_TRACKER_FOUND"
    log "SKIP: Torrent enthält bereits den Zieltracker: $TARGET_TRACKER_FOUND"
    exit 0
fi
log "Zieltracker nicht enthalten: OK"

# Release Group aus Torrent-Namen
RELEASE_GROUP="${TORRENT_NAME##*-}"
RELEASE_GROUP="$(printf '%s' "$RELEASE_GROUP" | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//;s/\.(mkv|mp4|m4v|avi|ts|m2ts)$//I')"
log "Erkannte Release Group: $RELEASE_GROUP"

for BANNED_GROUP in "${BANNED_GROUPS[@]}"; do
    if [[ "${RELEASE_GROUP,,}" == "${BANNED_GROUP,,}" ]]; then
        stats_event "skipped_group" "Banned Release Group: $RELEASE_GROUP"
        log "SKIP: Verbotene Release Group: $RELEASE_GROUP"
        exit 0
    fi
done

# Exakte Torrent-Struktur übernehmen.
SOURCE_BASENAME="$(basename -- "$CONTENT_PATH")"
TARGET_PATH="$UPLOAD_ROOT/$SOURCE_BASENAME"
if [[ -d "$CONTENT_PATH" ]]; then
    CONTENT_TYPE="directory"
elif [[ -f "$CONTENT_PATH" ]]; then
    CONTENT_TYPE="file"
else
    stats_event "failed" "Content Path ist weder Datei noch Verzeichnis"
    log "FEHLER: Ungültiger Content Path."
    exit 1
fi

log "Content Type: $CONTENT_TYPE"
log "Target Path:  $TARGET_PATH"

if [[ -e "$TARGET_PATH" ]]; then
    stats_event "skipped_existing" "Ziel existiert bereits: $TARGET_PATH" "$TARGET_PATH"
    log "SKIP: Ziel existiert bereits: $TARGET_PATH"
    exit 0
fi

# Hardlinks
if [[ "$CONTENT_TYPE" == "directory" ]]; then
    if ! cp -al -- "$CONTENT_PATH" "$UPLOAD_ROOT/"; then
        rm -rf -- "$TARGET_PATH" 2>/dev/null || true
        stats_event "failed" "Hardlink-Struktur konnte nicht erstellt werden" "$TARGET_PATH"
        log "FEHLER: Hardlink-Struktur konnte nicht erstellt werden."
        exit 1
    fi
else
    if ! ln -- "$CONTENT_PATH" "$TARGET_PATH"; then
        rm -f -- "$TARGET_PATH" 2>/dev/null || true
        stats_event "failed" "Single-File-Hardlink konnte nicht erstellt werden" "$TARGET_PATH"
        log "FEHLER: Hardlink konnte nicht erstellt werden."
        exit 1
    fi
fi

# Dateianzahl
if [[ "$CONTENT_TYPE" == "directory" ]]; then
    SOURCE_FILE_COUNT="$(find "$CONTENT_PATH" -type f -printf '.' | wc -c)"
    TARGET_FILE_COUNT="$(find "$TARGET_PATH" -type f -printf '.' | wc -c)"
else
    SOURCE_FILE_COUNT=1
    TARGET_FILE_COUNT=1
fi
if [[ "$SOURCE_FILE_COUNT" -ne "$TARGET_FILE_COUNT" ]]; then
    rm -rf -- "$TARGET_PATH" 2>/dev/null || true
    stats_event "failed" "Dateianzahl nach Hardlinking stimmt nicht überein" "$TARGET_PATH"
    log "FEHLER: Dateianzahl stimmt nicht überein."
    exit 1
fi

# Inodes prüfen
HARDLINK_ERROR=false
if [[ "$CONTENT_TYPE" == "directory" ]]; then
    while IFS= read -r -d '' SOURCE_FILE; do
        RELATIVE_PATH="${SOURCE_FILE#"$CONTENT_PATH"/}"
        TARGET_FILE="$TARGET_PATH/$RELATIVE_PATH"
        if [[ ! -f "$TARGET_FILE" ]] || [[ "$(stat -c '%d:%i' "$SOURCE_FILE")" != "$(stat -c '%d:%i' "$TARGET_FILE")" ]]; then
            HARDLINK_ERROR=true
            log "FEHLER: Kein Hardlink: $RELATIVE_PATH"
            break
        fi
    done < <(find "$CONTENT_PATH" -type f -print0)
else
    [[ "$(stat -c '%d:%i' "$CONTENT_PATH")" != "$(stat -c '%d:%i' "$TARGET_PATH")" ]] && HARDLINK_ERROR=true
fi
if [[ "$HARDLINK_ERROR" == "true" ]]; then
    rm -rf -- "$TARGET_PATH" 2>/dev/null || true
    stats_event "failed" "Hardlink-Verifikation fehlgeschlagen" "$TARGET_PATH"
    exit 1
fi

stats_event "hardlinked" "" "$TARGET_PATH"
log "Hardlink-Verifikation: OK"

# upPollo starten. Tags gilt für den von upPollo hinzugefügten Torrent.
UPPOLLO_RESPONSE="$(curl -fsS -X POST "$UPPOLLO_URL" \
    --data-urlencode "APIKey=$UPPOLLO_API_KEY" \
    --data-urlencode "Path=$TARGET_PATH" \
    --data-urlencode "Category=$CATEGORY" \
    --data-urlencode "Tags=$UPLOAD_TAG" \
    --data-urlencode "AutoTMM=false")"
UPPOLLO_RC=$?
if [[ "$UPPOLLO_RC" -ne 0 ]]; then
    stats_event "failed" "upPollo API-Aufruf fehlgeschlagen" "$TARGET_PATH"
    log "FEHLER: upPollo API-Aufruf fehlgeschlagen."
    exit 1
fi

log "upPollo Response: $UPPOLLO_RESPONSE"
UPPOLLO_STATUS="$(printf '%s' "$UPPOLLO_RESPONSE" | jq -r '.status // empty' 2>/dev/null || true)"
if [[ "$UPPOLLO_STATUS" != "queued" ]]; then
    stats_event "failed" "upPollo antwortete nicht mit status=queued" "$TARGET_PATH"
    log "WARNUNG: upPollo meldet nicht status=queued."
    exit 1
fi

# queued != uploaded. Den finalen Status übernimmt der upPollo-Log-Watcher.
stats_event "queued" "upPollo Job angenommen" "$TARGET_PATH"

{
    echo "date=$(date '+%Y-%m-%d %H:%M:%S')"
    echo "hash=$TORRENT_HASH"
    echo "torrent=$TORRENT_NAME"
    echo "category=$CATEGORY"
    echo "source=$CONTENT_PATH"
    echo "target=$TARGET_PATH"
} > "$STATE_FILE"
chmod 664 "$STATE_FILE" 2>/dev/null || true

find "$STATE_ROOT" -type f -name '*.submitted' -mtime +30 -delete 2>/dev/null || true
log "QUEUED: upPollo hat den Job angenommen. Finalstatus kommt aus dem upPollo-Log."
log "============================================================"
exit 0
