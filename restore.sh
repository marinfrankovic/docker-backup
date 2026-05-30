#!/bin/sh
# Restore from a backup run produced by backup.sh — end to end, overwrite.
#
# Fully automated overwrite restore of every project (or one project) inside a
# backup run:
#   1. stop every container that belongs to the project
#   2. overwrite each named volume AND bind-mounted host path with the snapshot
#   2b. recreate any MISSING containers from the saved compose file (this makes a
#       "from zero" restore on a fresh Docker engine work end to end)
#   3. start the containers again (database containers first)
#   4. re-import the SQL dump ONLY where no volume/bind archive covered the data
#      (when a data archive exists the data directory is already restored,
#       so a second SQL import is skipped to avoid conflicts)
#
# Two supported scenarios:
#   * Containers still exist  -> they are stopped, overwritten, and restarted.
#   * Fresh/empty Docker host -> containers are recreated from the saved compose
#     file with restored volumes + bind config. (Large media excluded at backup
#     time must be restored separately by the operator.)
#
# Usage:
#   restore.sh <run_subpath> [project] [container]   # whole run, one project, or one container
#   restore.sh --list                    # list runs/projects found on disk
#
#   run_subpath : path of the backup run, RELATIVE to the backups root, e.g.
#                 "prod/sched-a1b2/2026-05-29_030000". Legacy day folders such as
#                 "2026-05-26" also work.
#   project     : restore only this compose project from the run.
#   container   : with a project, restore only this one container from it.
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
CONTAINER_FILTER="${3:-}"
RUN="$BACKUP_ROOT_CONTAINER/$RUN_SUBPATH"
RUNHOST="$BACKUP_ROOT_HOST/$RUN_SUBPATH"
[ -d "$RUN" ] || { log "No such backup run: $RUN_SUBPATH (try: restore.sh --list)"; exit 1; }

exists()   { docker inspect "$1" >/dev/null 2>&1; }
running()  { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
is_db()    { case "$1" in *mysql*|*mariadb*|*postgres*) return 0 ;; *) return 1 ;; esac; }
container_selected() { # <cname> -> 0 if it should be restored (honours CONTAINER_FILTER)
  [ -z "$CONTAINER_FILTER" ] && return 0
  [ "$1" = "$CONTAINER_FILTER" ] && return 0
  return 1
}
img_of()   { docker inspect -f '{{.Config.Image}}' "$1" 2>/dev/null; }
env_of()   { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
               | grep -E "^$2=" | head -1 | cut -d= -f2-; }

restore_project() { # <project_name>
  project="$1"
  base="$RUN/$project"
  basehost="$RUNHOST/$project"
  [ -d "$base" ] || { log "  no data for project '$project' in run; skipping"; return 0; }

  if [ -n "$CONTAINER_FILTER" ]; then
    [ -d "$base/$CONTAINER_FILTER" ] || { log "  no data for container '$CONTAINER_FILTER' in project '$project'; skipping"; return 0; }
    log "Restoring container '$CONTAINER_FILTER' (project '$project') from '$RUN_SUBPATH' (overwrite)"
  else
    log "Restoring project '$project' from '$RUN_SUBPATH' (end-to-end overwrite)"
  fi

  # Collect the container names to act on: those captured in the backup, plus any
  # live container that still shares this compose project (e.g. one-shot helpers).
  cnames=""
  for cdir in "$base"/*/; do
    [ -d "$cdir" ] || continue
    cn="$(basename "$cdir")"
    container_selected "$cn" || continue
    cnames="$cnames $cn"
  done
  if [ "$project" != "_standalone" ] && [ -z "$CONTAINER_FILTER" ]; then
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
  log "  Phase 2/4: restoring volumes & bind mounts (overwrite)"
  for cdir in "$base"/*/; do
    [ -d "$cdir" ] || continue
    cname="$(basename "$cdir")"
    container_selected "$cname" || continue
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

    # bind-mounted host paths captured in binds.tsv
    [ -f "$cdir/binds.tsv" ] || continue
    while IFS="$(printf '\t')" read -r arc src dst; do
      [ -z "$arc" ] && continue
      [ -f "$cdir/$arc" ] || { log "    WARN missing bind archive $arc"; continue; }
      sbase="$(basename "$src")"
      sdir="$(dirname "$src")"
      log "    bind <- $src (was $dst)"
      # Mount the PARENT dir; replace only the backed-up basename so unrelated
      # siblings in the same directory are left untouched.
      if ! docker run --rm \
            -v "$sdir":/to \
            -v "$chost":/from:ro \
            "$HELPER_IMAGE" \
            sh -c "rm -rf '/to/$sbase' 2>/dev/null; tar xzf '/from/$arc' -C /to" 2>/dev/null; then
        log "      WARN restore failed for bind $src"
      fi
    done < "$cdir/binds.tsv"
  done

  # --- Phase 2.5: recreate MISSING containers from the saved compose file ------
  # This is what makes a "from zero" restore work: on a fresh Docker engine the
  # containers don't exist yet, so there is nothing to start. We rebuild the
  # project's working dir (compose file + .env) and run `docker compose up -d`.
  # Volumes and bind mounts were already restored above, so the containers come
  # up with their real data. (Scenario 1 — containers already exist — skips this
  # entirely and uses the stop/overwrite/start path.)
  if [ -z "$CONTAINER_FILTER" ] && [ -f "$base/compose.meta" ]; then
    missing=0
    for cdir in "$base"/*/; do
      [ -d "$cdir" ] || continue
      exists "$(basename "$cdir")" || { missing=1; break; }
    done
    if [ "$missing" = "1" ]; then
      wdir="$(awk -F '\t' '$1=="working_dir"{print $2; exit}' "$base/compose.meta")"
      cfiles="$(awk -F '\t' '$1=="compose_file"{print $2}' "$base/compose.meta")"
      if [ -n "$wdir" ] && [ -n "$cfiles" ]; then
        log "  Phase 2.5/4: recreating missing containers via 'docker compose up' (project '$project')"
        # Reconstruct the project dir INSIDE this container at its ORIGINAL
        # absolute path, so compose resolves relative bind paths (./conf) to the
        # SAME host paths we just restored, and finds the project's .env.
        mkdir -p "$wdir"
        composeargs=""
        for cf in $cfiles; do
          [ -f "$base/$cf" ] || continue
          cp -f "$base/$cf" "$wdir/$cf"
          composeargs="$composeargs -f $wdir/$cf"
        done
        [ -f "$base/.env" ] && cp -f "$base/.env" "$wdir/.env"

        # Also write the compose file(s) + .env back to the HOST project dir, so
        # the stack can be managed normally (docker compose up/down) after a bare
        # metal recovery. Best effort.
        whost="$(dirname "$wdir")"; wbase="$(basename "$wdir")"
        docker run --rm -v "$whost":/host "$HELPER_IMAGE" sh -c "mkdir -p '/host/$wbase'" 2>/dev/null || true
        for cf in $cfiles; do
          docker run --rm -v "$basehost":/src:ro -v "$whost":/host "$HELPER_IMAGE" \
            sh -c "[ -f '/src/$cf' ] && cp -f '/src/$cf' '/host/$wbase/$cf'" 2>/dev/null || true
        done
        docker run --rm -v "$basehost":/src:ro -v "$whost":/host "$HELPER_IMAGE" \
          sh -c "[ -f /src/.env ] && cp -f /src/.env '/host/$wbase/.env'" 2>/dev/null || true

        if [ -n "$composeargs" ]; then
          if docker compose --project-directory "$wdir" -p "$project" $composeargs up -d 2>&1 | sed 's/^/      /'; then
            log "    compose up complete"
          else
            log "    WARN compose up reported errors (see above)"
          fi
        fi
      else
        log "  Phase 2.5/4: containers missing but compose.meta is incomplete; cannot auto-recreate '$project'"
      fi
    fi
  fi

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
    container_selected "$cname" || continue

    sql=""
    for s in "$cdir"all-databases.sql.gz "$cdir"*.sql.gz; do
      [ -f "$s" ] && { sql="$s"; break; }
    done
    [ -z "$sql" ] && continue

    has_vol=0
    for v in "$cdir"volume-*.tar.gz; do [ -f "$v" ] && has_vol=1; done
    [ -f "$cdir/binds.tsv" ] && has_vol=1
    if [ "$has_vol" = "1" ]; then
      log "    $cname: data restored from volume/bind archive; skipping SQL re-import"
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
