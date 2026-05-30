#!/bin/sh
# One-shot Docker backup into a specific run directory.
#
# Usage:
#   backup.sh <run_subpath>                 # back up ALL containers (running + stopped)
#   backup.sh <run_subpath> --running       # back up only RUNNING containers
#   backup.sh <run_subpath> <name> [name..] # back up only the named containers
#                                           # (stopped ones: volumes + manifest only)
#
#   run_subpath : path of THIS backup run, RELATIVE to the backups root, e.g.
#                 "prod/sched-a1b2/2026-05-29_030000" or "_manual/2026-05-29_141500".
#
# For each target container it writes, under
#   $BACKUP_ROOT_CONTAINER/<run_subpath>/<project>/<container>/ :
#     inspect.json              full container manifest
#     all-databases.sql.gz      logical DB dump (mysql/mariadb/postgres)
#     volume-<name>.tar.gz      one archive per named volume
#     bind-<dest>.tar.gz        one archive per bind-mounted host path
#     binds.tsv                 map of bind archive -> host source + destination
# plus, per project: the compose file(s), the project .env (if any), and a
# compose.meta (original working dir + compose file names for from-scratch
# restore), and a _BACKUP_OK.txt marker for the run.
#
# Retention/pruning is handled by the caller (app.py) per schedule, NOT here.
set -u

BACKUP_ROOT_CONTAINER="${BACKUP_ROOT_CONTAINER:-/backups}"
BACKUP_ROOT_HOST="${BACKUP_ROOT_HOST:?Set BACKUP_ROOT_HOST to the host path of the backups dir}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"
SELF_NAME="${SELF_NAME:-docker-backup}"
HELPER_LABEL="docker-backup-helper=1"

# Volumes whose names match any of these space-separated glob patterns are NEVER
# archived. These are large media-library content volumes (movies / TV / music /
# downloads) that must be backed up separately, not bundled into app backups.
# Override with EXCLUDE_VOLUME_PATTERNS="" to disable, or a custom list.
EXCLUDE_VOLUME_PATTERNS="${EXCLUDE_VOLUME_PATTERNS-*movies* *movie* *tv* *shows* *series* *media* *music* *anime* *downloads* *torrents* remote_*}"

# Bind mounts (host paths mounted into a container, e.g. ./conf:/opt/app/conf)
# are archived too, so containers can be fully restored. The default list skips
# the Docker socket and pseudo/host system files (never useful to restore) plus
# the same large media-library paths as above. A pattern is matched against BOTH
# the host source path and the in-container destination path.
# Override with EXCLUDE_BIND_PATTERNS="" to disable, or a custom list.
EXCLUDE_BIND_PATTERNS="${EXCLUDE_BIND_PATTERNS-*/docker.sock /run/docker.sock /var/run/docker.sock /proc /proc/* /sys /sys/* /dev /dev/* /etc/localtime /etc/timezone /etc/hostname /etc/hosts /etc/resolv.conf *movies* *movie* *tv* *shows* *series* *media* *music* *anime* *downloads* *torrents*}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

is_excluded_volume() { # <volume-name> -> 0 if it should be skipped
  _v="$1"
  for _pat in $EXCLUDE_VOLUME_PATTERNS; do
    case "$_v" in
      $_pat) return 0 ;;
    esac
  done
  return 1
}

is_excluded_bind() { # <source> <destination> -> 0 if it should be skipped
  _s="$1"; _d="$2"
  for _pat in $EXCLUDE_BIND_PATTERNS; do
    case "$_s" in $_pat) return 0 ;; esac
    case "$_d" in $_pat) return 0 ;; esac
  done
  return 1
}

[ "$#" -ge 1 ] || { echo "usage: backup.sh <run_subpath> [--running | container...]" >&2; exit 2; }
RUN_SUBPATH="$1"; shift

RUNNING_ONLY=0
if [ "${1:-}" = "--running" ]; then RUNNING_ONLY=1; shift; fi

DEST="$BACKUP_ROOT_CONTAINER/$RUN_SUBPATH"
DESTHOST="$BACKUP_ROOT_HOST/$RUN_SUBPATH"
mkdir -p "$DEST"

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
      if is_excluded_volume "$vol"; then
        log "    skip volume $vol (excluded media library)"
        continue
      fi
      log "    volume -> $vol"
      if ! docker run --rm --label "$HELPER_LABEL" \
            -v "$vol":/from:ro \
            -v "$chost":/to \
            "$HELPER_IMAGE" \
            tar czf "/to/volume-$vol.tar.gz" -C /from . 2>/dev/null; then
        log "    WARN archive failed for volume $vol"
      fi
    done
}

archive_binds() { # <cid> <cdest> <chost>
  cid="$1"; cdest="$2"; chost="$3"
  # Emit one line per bind mount: <source><TAB><destination>
  docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{"\t"}}{{.Destination}}{{println}}{{end}}{{end}}' "$cid" \
  | while IFS="$(printf '\t')" read -r src dst; do
      [ -z "$src" ] && continue
      # never recurse into the backups tree itself
      case "$src" in
        "$BACKUP_ROOT_HOST"|"$BACKUP_ROOT_HOST"/*) log "    skip bind $src (inside backups root)"; continue ;;
      esac
      if is_excluded_bind "$src" "$dst"; then
        log "    skip bind $src (excluded)"
        continue
      fi
      sbase="$(basename "$src")"
      sdir="$(dirname "$src")"
      # archive name derived from the in-container destination path
      san="$(printf '%s' "$dst" | sed 's#^/##; s#[/ ]#_#g; s#[^A-Za-z0-9._-]#_#g')"
      [ -z "$san" ] && san="root"
      arc="bind-$san.tar.gz"
      log "    bind -> $src (at $dst)"
      # Mount the PARENT dir and archive just the basename, so this works for
      # both directory and single-file binds without touching siblings.
      if docker run --rm --label "$HELPER_LABEL" \
            -v "$sdir":/from:ro \
            -v "$chost":/to \
            "$HELPER_IMAGE" \
            tar czf "/to/$arc" -C /from "$sbase" 2>/dev/null; then
        printf '%s\t%s\t%s\n' "$arc" "$src" "$dst" >> "$cdest/binds.tsv"
      else
        log "    WARN archive failed for bind $src"
      fi
    done
}

copy_compose() { # <cid> <pdest> <phost>
  cid="$1"; pdest="$2"; phost="$3"
  cfg="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null)"
  [ -z "$cfg" ] && return 0
  wdir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$cid" 2>/dev/null)"

  # Record compose metadata ONCE per project (the original working directory and
  # the compose file names). A from-scratch restore reads this to recreate every
  # container with `docker compose up -d`, so a stack comes back even on a brand
  # new Docker engine with nothing running.
  if [ ! -f "$pdest/compose.meta" ]; then
    {
      printf 'working_dir\t%s\n' "$wdir"
      echo "$cfg" | tr ',' '\n' | while read -r f; do
        [ -z "$f" ] && continue
        printf 'compose_file\t%s\n' "$(basename "$f")"
      done
    } > "$pdest/compose.meta"
    # Back up the project's .env (a plain file in the working dir, not a mount),
    # so compose variable interpolation works on restore. The helper runs on the
    # host daemon, so its destination mount must be the HOST backup path.
    if [ -n "$wdir" ]; then
      docker run --rm --label "$HELPER_LABEL" -v "$wdir":/src:ro -v "$phost":/dst "$HELPER_IMAGE" \
        sh -c "[ -f /src/.env ] && cp -f /src/.env /dst/.env 2>/dev/null" 2>/dev/null || true
    fi
  fi

  echo "$cfg" | tr ',' '\n' | while read -r f; do
    [ -z "$f" ] && continue
    base="$(basename "$f")"
    dir="$(dirname "$f")"
    docker run --rm --label "$HELPER_LABEL" -v "$dir":/src:ro -v "$phost":/dst "$HELPER_IMAGE" \
      sh -c "cp -f '/src/$base' '/dst/$base' 2>/dev/null" 2>/dev/null || true
  done
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
  phost="$DESTHOST/$project"
  mkdir -p "$cdest"
  log "  container: $name (project=$project, image=$image)"
  docker inspect "$cid" > "$cdest/inspect.json" 2>/dev/null || true
  dump_database "$cid" "$name" "$image" "$cdest"
  archive_volumes "$cid" "$cdest" "$chost"
  archive_binds "$cid" "$cdest" "$chost"
  copy_compose "$cid" "$pdest" "$phost"
}

if [ "$#" -gt 0 ]; then
  log "===== Backup start (selected: $*) -> $DEST ====="
  for arg in "$@"; do
    cid="$(docker inspect --type container -f '{{.Id}}' "$arg" 2>/dev/null)"
    if [ -z "$cid" ]; then log "  WARN no such container: $arg"; continue; fi
    backup_one "$cid"
  done
elif [ "$RUNNING_ONLY" -eq 1 ]; then
  log "===== Backup start (all running) -> $DEST ====="
  for cid in $(docker ps -q); do
    backup_one "$cid"
  done
else
  log "===== Backup start (all containers) -> $DEST ====="
  for cid in $(docker ps -aq); do
    backup_one "$cid"
  done
fi

echo "backup_completed=$(date '+%Y-%m-%d %H:%M:%S')" > "$DEST/_BACKUP_OK.txt"
log "===== Backup complete: $DEST ====="
