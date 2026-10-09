#!/bin/sh
# Starts and stops an Unraid Docker container for llama-swap, through the Unraid API (GraphQL).
#
#   unraid-container.sh run  NAME        Stop the other GPU containers, start NAME, then keep running
#                                        while NAME runs. llama-swap treats this process as the model.
#   unraid-container.sh stop NAME [PID]  Stop NAME and wait until it has stopped; then end the
#                                        `run` process (PID, given by llama-swap as ${PID}).
#
# Settings come from /etc/llama-swap/unraid.env (override with UNRAID_ENV):
#   UNRAID_URL      e.g. http://192.168.1.20   (the /graphql path is added here)
#   UNRAID_API_KEY  key with DOCKER read + update permission only
#   GPU_CONTAINERS  space-separated container names that use the GPU
#   POLL_S          seconds between state checks while running (default 5)
#   CURL_OPTS       extra curl options, e.g. "-k" for a self-signed certificate
# Needs: sh, curl, jq.
set -u
. "${UNRAID_ENV:-/etc/llama-swap/unraid.env}"
POLL_S=${POLL_S:-5}
CURL_OPTS=${CURL_OPTS:-}

log() { echo "[unraid-container] $*" >&2; }

# Sends one GraphQL document; prints the JSON answer. Fails on network, HTTP or GraphQL errors.
gql() {
  # shellcheck disable=SC2086
  out=$(jq -nc --arg q "$1" '{query: $q}' | curl -fsS --max-time 30 $CURL_OPTS \
        -H "x-api-key: $UNRAID_API_KEY" -H "content-type: application/json" \
        --data @- "$UNRAID_URL/graphql") || return 1
  if [ "$(printf '%s' "$out" | jq '(.errors // []) | length')" != "0" ]; then
    log "Unraid API error: $(printf '%s' "$out" | jq -c '.errors')"
    return 1
  fi
  printf '%s' "$out"
}

# Prints "<id> <state>" for a container name.
# Returns 2 when the API cannot be reached, 3 when there is no such container.
info() {
  all=$(gql 'query { docker { containers { id names state } } }') || return 2
  line=$(printf '%s' "$all" | jq -r --arg n "$1" \
    '.data.docker.containers[] | select(any(.names[]; ltrimstr("/") == $n)) | "\(.id) \(.state)"' | head -n 1)
  if [ -z "$line" ]; then
    log "no container named '$1' on the Unraid server"
    return 3
  fi
  printf '%s\n' "$line"
}

mutate() {  # mutate start|stop ID
  gql "mutation { docker { $1(id: \"$2\") { id state } } }" >/dev/null
}

run() {
  for other in $GPU_CONTAINERS; do
    [ "$other" = "$NAME" ] && continue
    line=$(info "$other"); rc=$?
    [ $rc -eq 2 ] && { log "Unraid API unreachable"; exit 1; }
    [ $rc -ne 0 ] && continue
    set -- $line
    if [ "$2" = RUNNING ]; then
      log "stopping $other: the GPU is needed by $NAME"
      mutate stop "$1" || exit 1
    fi
  done

  line=$(info "$NAME") || exit 1
  set -- $line
  if [ "$2" != RUNNING ]; then
    log "starting $NAME"
    mutate start "$1" || exit 1
  else
    log "$NAME is already running"
  fi

  trap 'exit 0' TERM INT
  fails=0
  while :; do
    sleep "$POLL_S" & wait $!
    line=$(info "$NAME"); rc=$?
    if [ $rc -ne 0 ]; then
      fails=$((fails + 1))
      if [ $fails -ge 3 ]; then log "lost contact with the Unraid API"; exit 1; fi
      continue
    fi
    fails=0
    set -- $line
    if [ "$2" != RUNNING ]; then log "$NAME is $2"; exit 0; fi
  done
}

stop() {
  pid=${1:-}
  line=$(info "$NAME"); rc=$?
  if [ $rc -eq 0 ]; then
    set -- $line
    if [ "$2" = RUNNING ]; then
      log "stopping $NAME"
      mutate stop "$1" || exit 1
      i=0
      while [ $i -lt 30 ]; do
        line=$(info "$NAME") || break
        set -- $line
        [ "$2" = RUNNING ] || break
        sleep 2; i=$((i + 1))
      done
    fi
  fi
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null
  exit 0
}

[ $# -ge 2 ] || { echo "usage: $0 run|stop NAME [PID]" >&2; exit 2; }
ACTION=$1; NAME=$2; shift 2
case $ACTION in
  run) run ;;
  stop) stop "${1:-}" ;;
  *) echo "usage: $0 run|stop NAME [PID]" >&2; exit 2 ;;
esac
