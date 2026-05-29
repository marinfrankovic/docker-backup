#!/bin/sh
# One-shot Docker backup.
#
# Usage:
#   backup.sh                       # back up ALL running containers
#   backup.sh <name> [name...]      # back up only the named containers
#                                   # (stopped ones: volumes + manifest only)
#
# For each target container it writes, under
#   $BACKUP_ROOT_CONTAINER/<YYYY-MM-DD>/<project>/<container>/ :
#     inspect.json              full container manifest
#     all-databases.sql.gz      logical DB dump (mysql/mariadb/postgres)
#     volume-<name>.tar.gz      one archive per named volume
# plus the compose file(s) per project and a _BACKUP_OK.txt marker.
# Afterwards it prunes day-folders beyond RETENTION_DAYS.
set -u

RETENTION_DAYS="${RETENTION_DAYS:-7}"
BACKUP_ROOT_CONTAINER="${BACKUP_ROOT_CONTAINER:-/backups}"
BACKUP_ROOT_HOST="${BACKUP_ROOT_HOST:?Set BACKUP_ROOT_HOST to the host path of the backups dir}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"
SELF_NAME="${SELF_NAME:-docker-backup}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

is_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

env_val() { # <cid> <VARNAME>
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | grep -E "^$2=" | head -1 | cut -d= -f2-
}

dump_database() { # <cid> <name> <image> <cdest>
  cid="$1"; name="$2"; image="$3"; cdest="$4"
  case "$image" in
    *mysql*|*mariadb*|*postgres*) ;;
    *) return 0 ;;
  esac
  if ! is_running "$cid"; then
    log "    skip DB dump ($name not running); volume archive will cover data"
    return 0
  fi
  case "$image" in
    *mysql*|*mariadb*)
      rootpw="$(env_val "$cid" MYSQL_ROOT_PASSWORD)"
      user="$(env_val "$cid" MYSQL_USER)"
      upw="$(env_val "$cid" MYSQL_PASSWORD)"
      db="$(env_val "$cid" MYSQL_DATABASE)"
      if [ -n "$rootpw" ]; then
        log "    mysqldump (all databases) <- $name"
        if docker exec -e MP="$rootpw" "$cid" sh -c \
             'exec mysqldump -uroot -p"$MP" --all-databases --single-transaction --quick --routines --events --no-tablespaces' \
             > "$cdest/all-databases.sql" 2>/dev/null; then
          gzip -f "$cdest/all-databases.sql"
        else
          log "    WARN mysqldump failed for $name"; rm -f "$cdest/all-databases.sql"
        fi
      elif [ -n "$user" ] && [ -n "$upw" ] && [ -n "$db" ]; then
        log "    mysqldump (db $db) <- $name"
        if docker exec -e MP="$upw" "$cid" sh -c \
             "exec mysqldump -u$user -p\"\$MP\" --single-transaction --quick --routines --events --no-tablespaces $db" \
             > "$cdest/$db.sql" 2>/dev/null; then
          gzip -f "$cdest/$db.sql"
        else
          log "    WARN mysqldump failed for $name"; rm -f "$cdest/$db.sql"
        fi
      else
        log "    WARN no MySQL credentials found for $name; relying on volume archive"
      fi
      ;;
    *postgres*)
      puser="$(env_val "$cid" POSTGRES_USER)"; [ -z "$puser" ] && puser="postgres"
      log "    pg_dumpall <- $name"
      if docker exec "$cid" sh -c "exec pg_dumpall -U $puser" > "$cdest/all-databases.sql" 2>/dev/null; then
        gzip -f "$cdest/all-databases.sql"
      else
        log "    WARN pg_dumpall failed for $name"; rm -f "$cdest/all-databases.sql"
      fi
      ;;
  esac
}

archive_volumes() { # <cid> <cdest> <chost>
  cid="$1"; cdest="$2"; chost="$3"
  docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{println}}{{end}}{{end}}' "$cid" \
  | while read -r vol; do
      [ -z "$vol" ] && continue
      log "    volume -> $vol"
      if ! docker run --rm \
            -v "$vol":/from:ro \
            -v "$chost":/to \
            "$HELPER_IMAGE" \
            tar czf "/to/volume-$vol.tar.gz" -C /from . 2>/dev/null; then
        log "    WARN archive failed for volume $vol"
      fi
    done
}

copy_compose() { # <cid> <pdest>
  cid="$1"; pdest="$2"
  cfg="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null)"
  [ -z "$cfg" ] && return 0
  echo "$cfg" | tr ',' '\n' | while read -r f; do
    [ -z "$f" ] && continue
    base="$(basename "$f")"
    dir="$(dirname "$f")"
    docker run --rm -v "$dir":/src:ro -v "$pdest":/dst "$HELPER_IMAGE" \
      sh -c "cp -f '/src/$base' '/dst/$base' 2>/dev/null" 2>/dev/null || true
  done
}

prune_old() {
  cd "$BACKUP_ROOT_CONTAINER" || return 0
  count="$(ls -1d 20*/ 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${count:-0}" -gt "$RETENTION_DAYS" ]; then
    remove=$(( count - RETENTION_DAYS ))
    log "Retention: $count daily backups present, removing oldest $remove (keep $RETENTION_DAYS)"
    ls -1d 20*/ 2>/dev/null | sort | head -n "$remove" | while read -r old; do
      log "  prune $old"
      rm -rf "$old"
    done
  fi
}

backup_one() { # <cid>
  cid="$1"
  name="$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's#^/##')"
  [ -z "$name" ] && { log "  WARN unknown container '$cid'"; return 0; }
  [ "$name" = "$SELF_NAME" ] && { log "  skip self ($name)"; return 0; }
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$cid" 2>/dev/null)"
  [ -z "$project" ] && project="_standalone"
  image="$(docker inspect -f '{{.Config.Image}}' "$cid")"
  pdest="$DEST/$project"
  cdest="$pdest/$name"
  chost="$DESTHOST/$project/$name"
  mkdir -p "$cdest"
  log "  container: $name (project=$project, image=$image)"
  docker inspect "$cid" > "$cdest/inspect.json" 2>/dev/null || true
  dump_database "$cid" "$name" "$image" "$cdest"
  archive_volumes "$cid" "$cdest" "$chost"
  copy_compose "$cid" "$pdest"
}

day="$(date '+%Y-%m-%d')"
DEST="$BACKUP_ROOT_CONTAINER/$day"
DESTHOST="$BACKUP_ROOT_HOST/$day"
mkdir -p "$DEST"

if [ "$#" -gt 0 ]; then
  log "===== Backup start (selected: $*) -> $DEST ====="
  for arg in "$@"; do
    cid="$(docker inspect --type container -f '{{.Id}}' "$arg" 2>/dev/null)"
    if [ -z "$cid" ]; then log "  WARN no such container: $arg"; continue; fi
    backup_one "$cid"
  done
else
  log "===== Backup start (all running) -> $DEST ====="
  for cid in $(docker ps -q); do
    backup_one "$cid"
  done
fi

echo "backup_completed=$(date '+%Y-%m-%d %H:%M:%S')" > "$DEST/_BACKUP_OK.txt"
log "===== Backup complete: $DEST ====="
prune_old
