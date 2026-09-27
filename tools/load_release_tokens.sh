#!/usr/bin/env bash
# Source this file from the short-lived shell that runs a release operation.
# The token value is captured by command substitution and is never passed to
# bao-as as an argument or written to standard output.

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    printf '%s\n' 'source tools/load_release_tokens.sh from the release shell' >&2
    exit 2
fi

# A caller may have enabled tracing before sourcing this helper. Do not allow
# shell tracing to disclose a loaded credential.
set +x

release_token_cleanup() {
    unset FORGEJO_TOKEN ARGO_TOKEN ARGO_SUBMIT_TOKEN
}

# Keep the variables out of the shell environment after the release shell
# exits, including when it is interrupted during a network operation.
trap 'release_token_cleanup' EXIT HUP INT TERM

load_release_token() {
    if (($# != 2)); then
        printf '%s\n' 'usage: load_release_token ENVIRONMENT_VARIABLE OPENBAO_PATH' >&2
        return 2
    fi

    local env_name=$1 path=$2 value
    case $env_name in
        FORGEJO_TOKEN|ARGO_TOKEN|ARGO_SUBMIT_TOKEN)
            ;;
        *)
            printf '%s\n' "unsupported release token variable: ${env_name}" >&2
            return 2
            ;;
    esac

    # Remove an older value before looking up its replacement. A failed or
    # empty lookup must not leave a stale credential available to the caller.
    unset "$env_name"

    if ! value="$(bao-as rs-manager bao kv get -field=token "$path")"; then
        printf '%s\n' "token lookup failed for ${env_name}" >&2
        unset value
        return 1
    fi
    if [[ -z $value ]]; then
        printf '%s\n' "empty token for ${env_name}" >&2
        unset value
        return 1
    fi

    printf -v "$env_name" '%s' "$value"
    export "$env_name"
    unset value
}
