#!/usr/bin/env bash
# Deploy (or redeploy) BanTheFake on the VPS, with automatic rollback.
#
#   cd /opt/banthefake/deploy/hetzner && ./deploy.sh <git-ref>      e.g. ./deploy.sh v1.0.0  or  ./deploy.sh main
#
# A same-ref run skips build/pull/up when both services pass end-to-end health checks and the expanded
# Compose configuration hash matches. A changed or unhealthy stack is rebuilt and restarted. Success requires
# both services running, the expected model state on the app port, and Caddy serving HTTPS (or its HTTP-to-HTTPS
# redirect while the first certificate is pending).
#
# Before changing anything, the script tags the immutable image IDs of the running app and Caddy containers.
# A failed deploy restores exactly those images. If no stack was running, failure stops the attempted stack.
#
# Run as the deploy user (member of the `docker` group). Environment overrides: HEALTH_TIMEOUT,
# CERT_TIMEOUT, HEALTH_INTERVAL, REMOTE.
set -euo pipefail

# Everything lives in functions and runs from the last line: bash reads a script while executing it, and
# `git checkout` may replace this very file mid-run.

die() { echo "deploy: ERROR: $*" >&2; exit 1; }
say() { echo "deploy: $*"; }

# Value of KEY in .env (last assignment wins, surrounding quotes removed). Never sources the file.
env_get() {
    local line
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" .env | tail -n 1 || true)
    line=${line#*=}
    line=${line%$'\r'}
    line=${line#\"}; line=${line%\"}
    line=${line#\'}; line=${line%\'}
    printf '%s' "$line"
}

# True for an unset value or one that still points at a placeholder / reserved example domain.
is_placeholder() {
    local v=${1,,}
    [ -z "$v" ] && return 0
    case "$v" in
        *your-domain* | *change-me* | *replace* ) return 0 ;;
    esac
    local host=${v#https://}
    host=${host#*@}
    host=${host%%[/?#:]*}
    host=${host%.}
    [[ "$host" =~ (^|\.)(example(\.(com|org|net))?|invalid|test|localhost)$ ]]
}

check_env() {
    [ -f .env ] || die ".env not found in $PWD. Run: cp .env.example .env && chmod 600 .env, then edit it."
    if [ -n "$(find .env -perm /044 2>/dev/null)" ]; then
        say "warning: .env is readable by other users; run: chmod 600 .env"
    fi
    local domain acme token contact env model
    domain=$(env_get BTF_DOMAIN)
    acme=$(env_get ACME_EMAIL)
    token=$(env_get BTF_ADMIN_TOKEN)
    contact=$(env_get BTF_PRIVACY_CONTACT)
    env=$(env_get BTF_ENV)
    local errors=0
    if is_placeholder "$domain"; then echo "  BTF_DOMAIN is unset or a placeholder" >&2; errors=1; fi
    if is_placeholder "$acme" || [[ "$acme" != *@* ]]; then
        echo "  ACME_EMAIL is unset, not an email address, or a placeholder" >&2; errors=1
    fi
    if [ "${#token}" -lt 24 ]; then
        echo "  BTF_ADMIN_TOKEN must be at least 24 characters (python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >&2
        errors=1
    fi
    if is_placeholder "$contact"; then
        echo "  BTF_PRIVACY_CONTACT is unset or a placeholder (a monitored email address or https:// URL)" >&2
        errors=1
    fi
    [ "$errors" -eq 0 ] || die ".env is incomplete (see above)."
    [ "${env,,}" = production ] || say "warning: BTF_ENV is not 'production' in .env; startup checks are relaxed."
    model=$(env_get BTF_MODEL)
    case "${model,,}" in off | 0 | false | no) EXPECTED_MODEL=off ;; *) EXPECTED_MODEL=loaded ;; esac
    DOMAIN=$domain
}

# Resolve a branch name to the fetched remote branch, anything else (tag, sha) as given.
resolve_ref() {
    local ref=$1
    if git rev-parse --verify --quiet "refs/remotes/$REMOTE/$ref^{commit}" >/dev/null; then
        git rev-parse "refs/remotes/$REMOTE/$ref^{commit}"
    elif git rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
        git rev-parse "$ref^{commit}"
    else
        die "unknown git ref '$ref' (after fetching $REMOTE)"
    fi
}

snapshot_service() {
    local service=$1 output cid running image tag
    local -a ids=()
    if ! output=$(docker compose ps -q "$service"); then
        die "could not inspect the running $service service"
    fi
    while IFS= read -r cid; do
        [ -n "$cid" ] && ids+=("$cid")
    done <<< "$output"
    [ "${#ids[@]}" -le 1 ] || die "expected one $service container, found ${#ids[@]}"
    [ "${#ids[@]}" -eq 1 ] || return 0
    cid=${ids[0]}
    if ! running=$(docker inspect --format '{{.State.Running}}' "$cid"); then
        die "could not inspect $service container $cid"
    fi
    [ "$running" = true ] || return 0
    if ! image=$(docker inspect --format '{{.Image}}' "$cid"); then
        die "could not read the immutable image ID for $service container $cid"
    fi
    tag="banthefake:rollback-${service}-${RUN_ID}"
    docker tag "$image" "$tag" || die "could not preserve $service image $image as $tag"
    case "$service" in
        app) PREV_APP_IMAGE=$image; PREV_APP_TAG=$tag ;;
        caddy) PREV_CADDY_IMAGE=$image; PREV_CADDY_TAG=$tag ;;
        *) die "internal error: unsupported service '$service'" ;;
    esac
    say "preserved running $service container $cid image $image as $tag"
}

snapshot_running_stack() {
    snapshot_service app
    snapshot_service caddy
}

service_running() {
    local service=$1 cid running
    cid=$(docker compose ps -q "$service" 2>/dev/null) || return 1
    [ -n "$cid" ] && [[ "$cid" != *$'\n'* ]] || return 1
    running=$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null) || return 1
    [ "$running" = true ]
}

services_running() {
    service_running app && service_running caddy
}

health_model() {
    curl -fsS --max-time 5 http://127.0.0.1:8000/api/health 2>/dev/null \
        | sed -nE 's/.*"model"[[:space:]]*:[[:space:]]*"([a-z]+)".*/\1/p' || true
}

probe_proxy() {
    local redirect status location
    if curl -fsS --max-time 10 --resolve "$DOMAIN:443:127.0.0.1" \
        "https://$DOMAIN/api/health" >/dev/null 2>&1; then
        PROXY_MODE=https
        return 0
    fi
    redirect=$(curl -sS --max-time 5 --resolve "$DOMAIN:80:127.0.0.1" -o /dev/null \
        -w '%{http_code} %{redirect_url}' "http://$DOMAIN/api/health" 2>/dev/null) || return 1
    status=${redirect%% *}
    location=${redirect#* }
    case "$status" in 301 | 302 | 307 | 308) ;; *) return 1 ;; esac
    [[ "$location" == "https://$DOMAIN/"* ]] || return 1
    PROXY_MODE=http-redirect
}

stack_ready_once() {
    LAST_MODEL=""
    PROXY_MODE=""
    services_running || return 1
    LAST_MODEL=$(health_model)
    [ "$LAST_MODEL" = "$EXPECTED_MODEL" ] || return 1
    probe_proxy
}

wait_ready() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) certificate_deadline=$((SECONDS + CERT_TIMEOUT))
    local redirect_ready=0
    say "waiting up to ${HEALTH_TIMEOUT}s for both services, model \"$EXPECTED_MODEL\", and Caddy..."
    while true; do
        if stack_ready_once; then
            if [ "$PROXY_MODE" = https ]; then
                say "healthy through app and Caddy HTTPS (model: $LAST_MODEL)"
                return 0
            fi
            redirect_ready=1
            if [ "$SECONDS" -ge "$certificate_deadline" ] || [ "$SECONDS" -ge "$deadline" ]; then
                say "healthy app and Caddy HTTP-to-HTTPS redirect; certificate issuance is still pending (model: $LAST_MODEL)"
                return 0
            fi
        fi
        [ "$SECONDS" -lt "$deadline" ] || break
        sleep "$HEALTH_INTERVAL"
    done
    if [ "$redirect_ready" -eq 1 ]; then
        say "healthy app and Caddy HTTP-to-HTTPS redirect; certificate issuance is still pending (model: $LAST_MODEL)"
        return 0
    fi
    say "not ready after ${HEALTH_TIMEOUT}s (both services running: $(services_running && echo yes || echo no); last model: ${LAST_MODEL:-no answer}; Caddy: ${PROXY_MODE:-no response})"
    return 1
}

container_label() {
    local service=$1 label=$2 cid
    cid=$(docker compose ps -q "$service" 2>/dev/null) || return 1
    [ -n "$cid" ] && [[ "$cid" != *$'\n'* ]] || return 1
    docker inspect --format "{{ index .Config.Labels \"$label\" }}" "$cid" 2>/dev/null
}

running_metadata_matches() {
    local service commit hash
    for service in app caddy; do
        commit=$(container_label "$service" org.banthefake.git-commit) || return 1
        hash=$(container_label "$service" org.banthefake.compose-hash) || return 1
        [ "$commit" = "$TARGET" ] && [ "$hash" = "$COMPOSE_HASH" ] || return 1
    done
}

compose_config_hash() {
    BTF_DEPLOY_COMMIT=$TARGET BTF_COMPOSE_HASH=hash-pending docker compose config \
        | sha256sum | awk '{print $1}'
}

write_rollback_override() {
    {
        echo 'services:'
        if [ -n "$PREV_APP_TAG" ]; then
            printf '  app:\n    image: %s\n' "$PREV_APP_TAG"
        fi
        if [ -n "$PREV_CADDY_TAG" ]; then
            printf '  caddy:\n    image: %s\n' "$PREV_CADDY_TAG"
        fi
    } > "$ROLLBACK_OVERRIDE"
}

rollback() {
    local prev=$1 failures=0 restored_health=0
    local -a restore_services=()
    set +e
    say "ROLLING BACK failed deploy of '$REF'"
    if ! docker compose logs --tail 50 app >&2; then
        say "ROLLBACK WARNING: app logs were unavailable"
    fi

    if ! docker compose down --remove-orphans; then
        say "ROLLBACK ERROR: could not fully stop the attempted stack"
        failures=1
    fi
    if ! git checkout --quiet --detach "$prev"; then
        say "ROLLBACK ERROR: could not restore checkout $prev"
        failures=1
    fi

    if [ -z "$PREV_APP_TAG" ] && [ -z "$PREV_CADDY_TAG" ]; then
        say "no services were running when this invocation started; the attempted first deployment was stopped"
    else
        [ -n "$PREV_APP_TAG" ] && restore_services+=(app)
        [ -n "$PREV_CADDY_TAG" ] && restore_services+=(caddy)
        if ! ROLLBACK_OVERRIDE=$(mktemp "${TMPDIR:-/tmp}/btf-rollback.XXXXXX.yml"); then
            say "ROLLBACK ERROR: could not allocate a rollback Compose override"
            failures=1
        elif ! write_rollback_override; then
            say "ROLLBACK ERROR: could not create the rollback Compose override"
            failures=1
        elif ! docker compose -f docker-compose.yml -f "$ROLLBACK_OVERRIDE" up -d --no-deps "${restore_services[@]}"; then
            say "ROLLBACK ERROR: could not start one or more preserved services"
            failures=1
        elif [ -n "$PREV_APP_TAG" ] && [ -n "$PREV_CADDY_TAG" ]; then
            if wait_ready; then
                restored_health=1
            else
                say "ROLLBACK ERROR: the restored stack did not pass end-to-end health checks"
                failures=1
            fi
        else
            say "restored the same partial service set that was running at invocation start: ${restore_services[*]}"
        fi
    fi

    say "rollback final state:"
    if ! docker compose ps; then
        say "ROLLBACK ERROR: could not print the final Compose state"
        failures=1
    fi
    if [ -n "$ROLLBACK_OVERRIDE" ] && ! rm -f "$ROLLBACK_OVERRIDE"; then
        say "ROLLBACK WARNING: could not remove temporary override $ROLLBACK_OVERRIDE"
    fi

    if [ "$failures" -eq 0 ] && [ "$restored_health" -eq 1 ]; then
        say "rollback restored the exact app image $PREV_APP_IMAGE and Caddy image $PREV_CADDY_IMAGE; the stack is healthy"
    elif [ "$failures" -eq 0 ] && [ -z "$PREV_APP_TAG$PREV_CADDY_TAG" ]; then
        say "first deployment failed; no stale image was started"
    elif [ "$failures" -ne 0 ]; then
        say "ROLLBACK FAILED; inspect the final state and logs above"
    fi
    exit 1
}

remove_snapshot_tags() {
    local tag
    for tag in "$PREV_APP_TAG" "$PREV_CADDY_TAG"; do
        [ -n "$tag" ] || continue
        if ! docker image rm "$tag" >/dev/null; then
            say "warning: deployed successfully but could not remove temporary tag $tag"
        fi
    done
}

main() {
    [ $# -eq 1 ] || die "usage: $0 <git-ref>   (tag, commit or branch, e.g. v1.0.0 or main)"
    REF=$1
    REMOTE=${REMOTE:-origin}
    HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-600}
    CERT_TIMEOUT=${CERT_TIMEOUT:-60}
    HEALTH_INTERVAL=${HEALTH_INTERVAL:-5}
    RUN_ID="$(date -u +%Y%m%d%H%M%S)-$$"
    PREV_APP_IMAGE=""; PREV_APP_TAG=""
    PREV_CADDY_IMAGE=""; PREV_CADDY_TAG=""
    ROLLBACK_OVERRIDE=""
    LAST_MODEL=""; PROXY_MODE=""

    cd "$(dirname "$0")"
    command -v docker >/dev/null || die "docker is not installed"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
    command -v curl >/dev/null || die "curl is not installed"
    command -v sha256sum >/dev/null || die "sha256sum is not installed"

    check_env

    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        die "the checkout has local changes to tracked files; commit or discard them first (git status)"
    fi

    snapshot_running_stack

    say "fetching $REMOTE"
    git fetch --quiet --tags --prune --force "$REMOTE"
    local prev
    prev=$(git rev-parse HEAD)
    TARGET=$(resolve_ref "$REF")
    say "current: $prev"
    say "target:  $TARGET ($REF)"

    if ! git checkout --quiet --detach "$TARGET"; then
        rollback "$prev"
    fi
    if ! COMPOSE_HASH=$(compose_config_hash); then
        say "could not calculate the expanded Compose configuration hash"
        rollback "$prev"
    fi
    export BTF_DEPLOY_COMMIT=$TARGET BTF_COMPOSE_HASH=$COMPOSE_HASH

    if running_metadata_matches && stack_ready_once; then
        say "same commit and Compose hash are already healthy; skipping build, pull, and up"
        remove_snapshot_tags
        docker compose ps
        say "deployed $REF ($TARGET)"
        say "live at: https://$DOMAIN/  (health: https://$DOMAIN/api/health)"
        return 0
    fi

    if ! docker compose build app || ! docker compose pull caddy; then
        rollback "$prev"
    fi
    if ! docker compose up -d || ! wait_ready; then
        rollback "$prev"
    fi

    remove_snapshot_tags
    docker compose ps
    say "deployed $REF ($TARGET)"
    say "live at: https://$DOMAIN/  (health: https://$DOMAIN/api/health)"
}

main "$@"
