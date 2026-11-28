# Shared settings for the laptop-side DigitalOcean scripts in this folder
# (docs/deploy-digitalocean.md). Sourced, never run. Override a value in the environment,
# for example: TUZ_DO_SIZE=s-4vcpu-8gb ops/digitalocean/create-droplet

TUZ_DO_NAME="${TUZ_DO_NAME:-tuz-garage}"
TUZ_DO_REGION="${TUZ_DO_REGION:-fra1}"
TUZ_DO_SIZE="${TUZ_DO_SIZE:-s-2vcpu-4gb}"
TUZ_DO_IMAGE="${TUZ_DO_IMAGE:-ubuntu-24-04-x64}"
TUZ_DO_DOMAIN="${TUZ_DO_DOMAIN:-garage1886.ge}"
# A key used for this server only. create-droplet makes it if it is missing.
TUZ_DO_SSH_KEY="${TUZ_DO_SSH_KEY:-$HOME/.ssh/tuz_do}"
# The server's address, its host key and transfer files in flight. Private to this user.
TUZ_DO_STATE="${TUZ_DO_STATE:-$HOME/.config/tuz/digitalocean}"
TUZ_DO_LOG_DIR="${TUZ_DO_LOG_DIR:-$HOME/.local/state/tuz/deploy}"
# The warehouse refuses addresses outside Georgia, so the server's sync reads it through
# this laptop (ops/digitalocean/warehouse-relay) on this loopback port of the server. Set it
# to empty once the warehouse allows the server's own address.
TUZ_DO_WAREHOUSE_RELAY_PORT="${TUZ_DO_WAREHOUSE_RELAY_PORT-16068}"

tuz_die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

tuz_repo_root() {
    git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel
}

tuz_host() {
    local file="$TUZ_DO_STATE/host"
    [[ -s "$file" ]] || tuz_die "no server address yet ($file): run ops/digitalocean/create-droplet"
    cat "$file"
}

# The first connection records the server's host key; a different key later is refused.
tuz_ssh_options=(
    -i "$TUZ_DO_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$TUZ_DO_STATE/known_hosts"
    -o ServerAliveInterval=30 -o ServerAliveCountMax=4
)

tuz_ssh() {
    local host
    host="$(tuz_host)"
    ssh "${tuz_ssh_options[@]}" "root@$host" "$@"
}

# rsync wants the remote shell as one string; none of these values contains a space.
tuz_rsync() {
    rsync -e "ssh ${tuz_ssh_options[*]}" "$@"
}

# Copy everything this script prints into a dated log file as well as the terminal.
tuz_log_to_file() {
    local name="$1" file
    mkdir -p "$TUZ_DO_LOG_DIR"
    chmod 700 "$TUZ_DO_LOG_DIR"
    file="$TUZ_DO_LOG_DIR/$(date +%Y-%m-%d_%H%M%S)_$name.log"
    exec > >(tee -a "$file") 2>&1
    printf 'log: %s\n' "$file"
}
