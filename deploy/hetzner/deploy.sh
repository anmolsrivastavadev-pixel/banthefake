#!/usr/bin/env bash
# Deploy (or redeploy) BanTheFake on the VPS, with automatic rollback.
#
#   cd /opt/banthefake/deploy/hetzner && ./deploy.sh <git-ref>      e.g. ./deploy.sh v1.0.0  or  ./deploy.sh main
#
# Idempotent: running it again with the same ref rebuilds nothing new and leaves the running stack as is.
# Steps: check .env -> fetch -> check out <git-ref> (detached) -> build the app image, pull Caddy ->
# `docker compose up -d` -> wait for /api/health to report the detector loaded -> print the URL.
# If the new version does not become healthy within HEALTH_TIMEOUT seconds (default 600), it checks the
# previous commit out again, restores the previous image and restarts it, then exits non-zero.
#
# Run as the deploy user (member of the `docker` group). Environment overrides: HEALTH_TIMEOUT, REMOTE.
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

health_model() {
    curl -fsS --max-time 5 http://127.0.0.1:8000/api/health 2>/dev/null \
        | sed -nE 's/.*"model"[[:space:]]*:[[:space:]]*"([a-z]+)".*/\1/p' || true
}

wait_healthy() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) model=""
    say "waiting up to ${HEALTH_TIMEOUT}s for /api/health to report model \"$EXPECTED_MODEL\"..."
    while [ "$SECONDS" -lt "$deadline" ]; do
        model=$(health_model)
        if [ "$model" = "$EXPECTED_MODEL" ]; then
            say "healthy (model: $model)"
            return 0
        fi
        sleep 5
    done
    say "not healthy after ${HEALTH_TIMEOUT}s (last model state: ${model:-no answer})"
    return 1
}

rollback() {
    local prev=$1
    say "ROLLING BACK to $prev"
    docker compose logs --tail 50 app >&2 || true
    git checkout --quiet --detach "$prev"
    if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then
        docker tag "$ROLLBACK_IMAGE" "$APP_IMAGE"
    else
        docker compose build app
    fi
    docker compose up -d
    if wait_healthy; then
        die "deploy of '$REF' failed; rolled back to $prev, which is healthy."
    fi
    die "deploy of '$REF' failed AND the rollback to $prev is not healthy. Check: docker compose logs app"
}

main() {
    [ $# -eq 1 ] || die "usage: $0 <git-ref>   (tag, commit or branch, e.g. v1.0.0 or main)"
    REF=$1
    REMOTE=${REMOTE:-origin}
    HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-600}
    APP_IMAGE=banthefake:local
    ROLLBACK_IMAGE=banthefake:rollback

    cd "$(dirname "$0")"
    command -v docker >/dev/null || die "docker is not installed"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
    command -v curl >/dev/null || die "curl is not installed"

    check_env

    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        die "the checkout has local changes to tracked files; commit or discard them first (git status)"
    fi

    say "fetching $REMOTE"
    git fetch --quiet --tags --prune --force "$REMOTE"
    local prev target
    prev=$(git rev-parse HEAD)
    target=$(resolve_ref "$REF")
    say "current: $prev"
    say "target:  $target ($REF)"

    # Keep the image that is running now, so a rollback does not need a rebuild.
    if docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then
        docker tag "$APP_IMAGE" "$ROLLBACK_IMAGE"
    fi

    git checkout --quiet --detach "$target"
    if ! docker compose build app || ! docker compose pull caddy; then
        rollback "$prev"
    fi
    if ! docker compose up -d || ! wait_healthy; then
        rollback "$prev"
    fi

    docker compose ps
    say "deployed $REF ($target)"
    say "live at: https://$DOMAIN/  (health: https://$DOMAIN/api/health)"
}

main "$@"
exit
