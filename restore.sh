#!/bin/sh
# Restore from a backup run produced by backup.sh — end to end, overwrite.
#
# Fully automated overwrite restore of every project (or one project) inside a
# backup run:
#   1. stop every container that belongs to the project
#   2. overwrite each named volume with the backed-up snapshot
#   3. start the containers again (database containers first)
#   4. re-import the SQL dump ONLY where no volume archive covered the data
#      (when a volume archive exists the data directory is already restored,
#       so a second SQL import is skipped to avoid conflicts)
#
# Usage:
#   restore.sh <run_subpath> [project]   # restore all projects, or just <project>
#   restore.sh --list                    # list runs/projects found on disk
#
#   run_subpath : path of the backup run, RELATIVE to the backups root, e.g.
#                 "prod/sched-a1b2/2026-05-29_030000". Legacy day folders such as
#                 "2026-05-26" also work.
set -u

BACKUP_ROOT_CONTAINER="${BACKUP_ROOT_CONTAINER:-/backups}"
BACKUP_ROOT_HOST="${BACKUP_ROOT_HOST:?Set BACKUP_ROOT_HOST}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"

log() { echo "[restore] $*"; }

if [ "${1:-}" = "--list" ] || [ -z "${1:-}" ]; then
  echo "Backup runs (folders containing _BACKUP_OK.txt):"
  find "$BACKUP_ROOT_CONTAINER" -name _BACKUP_OK.txt 2>/dev/null | sort | while read -r ok; do
    run="$(dirname "$ok")"
    rel="${run#$BACKUP_ROOT_CONTAINER/}"
    echo "  $rel:"
    for p in "$run"/*/; do [ -d "$p" ] && echo "    - $(basename "$p")"; done
  done
  exit 0
fi

RUN_SUBPATH="$1"
FILTER="${2:-}"
RUN="$BACKUP_ROOT_CONTAINER/$RUN_SUBPATH"
RUNHOST="$BACKUP_ROOT_HOST/$RUN_SUBPATH"
[ -d "$RUN" ] || { log "No such backup run: $RUN_SUBPATH (try: restore.sh --list)"; exit 1; }

exists()   { docker inspect "$1" >/dev/null 2>&1; }
running()  { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
is_db()    { case "$1" in *mysql*|*mariadb*|*postgres*) return 0 ;; *) return 1 ;; esac; }
img_of()   { docker inspect -f '{{.Config.Image}}' "$1" 2>/dev/null; }
env_of()   { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
               | grep -E "^$2=" | head -1 | cut -d= -f2-; }

restore_project() { # <project_name>
  project="$1"
  base="$RUN/$project"
  basehost="$RUNHOST/$project"
  [ -d "$base" ] || { log "  no data for project '$project' in run; skipping"; return 0; }

  log "Restoring project '$project' from '$RUN_SUBPATH' (end-to-end overwrite)"

  # Collect the container names to act on: those captured in the backup, plus any
  # live container that still shares this compose project (e.g. one-shot helpers).
  cnames=""
  for cdir in "$base"/*/; do
    [ -d "$cdir" ] || continue
    cnames="$cnames $(basename "$cdir")"
  done
  if [ "$project" != "_standalone" ]; then
    for n in $(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}}' 2>/dev/null); do
      case " $cnames " in *" $n "*) ;; *) cnames="$cnames $n" ;; esac
    done
  fi

  # --- Phase 1: stop the project's containers so volume writes are consistent --
  log "  Phase 1/4: stopping project containers"
  for n in $cnames; do
    if exists "$n" && running "$n"; then
      log "    stop $n"
      docker stop "$n" >/dev/null 2>&1 || log "      WARN could not stop $n"
    fi
  done

  # --- Phase 2: overwrite the named volumes with the backed-up data ------------
  log "  Phase 2/4: restoring volumes (overwrite)"
  for cdir in "$base"/*/; do
    [ -d "$cdir" ] || continue
    cname="$(basename "$cdir")"
    chost="$basehost/$cname"
    for arc in "$cdir"volume-*.tar.gz; do
      [ -f "$arc" ] || continue
      vol="$(basename "$arc" | sed 's/^volume-//; s/\.tar\.gz$//')"
      log "    volume <- $vol"
      docker volume create "$vol" >/dev/null 2>&1 || true
      if ! docker run --rm \
            -v "$vol":/to \
            -v "$chost":/from:ro \
            "$HELPER_IMAGE" \
            sh -c "rm -rf /to/* /to/..?* /to/.[!.]* 2>/dev/null; tar xzf '/from/$(basename "$arc")' -C /to" 2>/dev/null; then
        log "      WARN restore failed for volume $vol"
      fi
    done
  done

  # --- Phase 3: start the containers again (databases first) -------------------
  log "  Phase 3/4: starting project containers"
  for n in $cnames; do
    exists "$n" || continue
    if is_db "$(img_of "$n")"; then
      log "    start (db) $n"
      docker start "$n" >/dev/null 2>&1 || log "      WARN could not start $n"
    fi
  done
  for n in $cnames; do
    exists "$n" || continue
    is_db "$(img_of "$n")" && continue
    log "    start $n"
    docker start "$n" >/dev/null 2>&1 || log "      WARN could not start $n"
  done

  # --- Phase 4: SQL re-import only where no volume archive covered the data -----
  log "  Phase 4/4: database re-import (where needed)"
  for cdir in "$base"/*/; do
    [ -d "$cdir" ] || continue
    cname="$(basename "$cdir")"

    sql=""
    for s in "$cdir"all-databases.sql.gz "$cdir"*.sql.gz; do
      [ -f "$s" ] && { sql="$s"; break; }
    done
    [ -z "$sql" ] && continue

    has_vol=0
    for v in "$cdir"volume-*.tar.gz; do [ -f "$v" ] && has_vol=1; done
    if [ "$has_vol" = "1" ]; then
      log "    $cname: data restored from volume archive; skipping SQL re-import"
      continue
    fi

    if ! exists "$cname"; then
      log "    $cname: no live container; $(basename "$sql") left for manual import"
      continue
    fi

    image="$(img_of "$cname")"
    case "$image" in
      *mysql*|*mariadb*)
        rootpw="$(env_of "$cname" MYSQL_ROOT_PASSWORD)"
        if [ -z "$rootpw" ]; then
          log "    $cname: no MYSQL_ROOT_PASSWORD; skipping ($(basename "$sql") left in place)"
          continue
        fi
        log "    $cname: waiting for MySQL to accept connections..."
        i=0
        while [ "$i" -lt 60 ]; do
          docker exec -e MP="$rootpw" "$cname" sh -c 'mysqladmin ping -uroot -p"$MP" --silent' >/dev/null 2>&1 && break
          i=$((i + 1)); sleep 2
        done
        log "    $cname: importing $(basename "$sql")"
        gzip -dc "$sql" | docker exec -i -e MP="$rootpw" "$cname" sh -c 'exec mysql -uroot -p"$MP"' \
          && log "      import OK" || log "      WARN import failed"
        ;;
      *postgres*)
        puser="$(env_of "$cname" POSTGRES_USER)"; [ -z "$puser" ] && puser="postgres"
        log "    $cname: waiting for Postgres to accept connections..."
        i=0
        while [ "$i" -lt 60 ]; do
          docker exec "$cname" sh -c "pg_isready -U $puser" >/dev/null 2>&1 && break
          i=$((i + 1)); sleep 2
        done
        log "    $cname: importing $(basename "$sql") (postgres)"
        gzip -dc "$sql" | docker exec -i "$cname" sh -c "exec psql -U $puser" \
          && log "      import OK" || log "      WARN import failed"
        ;;
      *)
        log "    $cname: not a database image; $(basename "$sql") left in place"
        ;;
    esac
  done

  log "Restore of '$project' complete. Containers are running with restored data."
}

if [ -n "$FILTER" ]; then
  restore_project "$FILTER"
else
  any=0
  for pdir in "$RUN"/*/; do
    [ -d "$pdir" ] || continue
    pname="$(basename "$pdir")"
    case "$pname" in _*) continue ;; esac
    any=1
    restore_project "$pname"
  done
  [ "$any" = "0" ] && { log "No projects found in run '$RUN_SUBPATH'."; exit 1; }
fi

log "Restore complete for run '$RUN_SUBPATH'."
