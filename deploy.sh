#!/usr/bin/env bash
# net-watch — generic Ansible deploy wrapper.
#
# Deployment-agnostic on purpose: no hardcoded paths, hosts, or usernames,
# so this is safe to keep in the public repo. Point it at your own Ansible
# project and host via environment variables.
#
# Required:
#   NET_WATCH_IAC_DIR   Path to your Ansible project (the one with
#                        playbooks/site.yml and a net_watch tag/role).
#   NET_WATCH_HOST       SSH target for the post-deploy status check,
#                        e.g. user@host.
#
# Optional:
#   NET_WATCH_PLAYBOOK       Playbook path relative to NET_WATCH_IAC_DIR
#                             (default: playbooks/site.yml)
#   NET_WATCH_ANSIBLE_TAGS   Ansible tags to target (default: net_watch)
#   NET_WATCH_SERVICE        systemd unit name to check after deploy
#                             (default: net-watch)
#
# Usage:
#   NET_WATCH_IAC_DIR=/path/to/homelab-iac NET_WATCH_HOST=user@host ./deploy.sh
#   ./deploy.sh --yes     # skip the confirmation prompt (e.g. in CI)

set -euo pipefail

usage() {
    cat <<EOF
Usage: NET_WATCH_IAC_DIR=<path> NET_WATCH_HOST=<user@host> $0 [--yes]

Required environment variables:
  NET_WATCH_IAC_DIR   Path to the Ansible project (contains playbooks/site.yml)
  NET_WATCH_HOST       SSH target for the post-deploy status check (user@host)

Optional environment variables:
  NET_WATCH_PLAYBOOK       default: playbooks/site.yml
  NET_WATCH_ANSIBLE_TAGS   default: net_watch
  NET_WATCH_SERVICE        default: net-watch

Options:
  --yes   apply without prompting for confirmation after the dry run
  -h, --help
EOF
}

confirm=false
for arg in "$@"; do
    case "$arg" in
        --yes) confirm=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

: "${NET_WATCH_IAC_DIR:?NET_WATCH_IAC_DIR is required — see --help}"
: "${NET_WATCH_HOST:?NET_WATCH_HOST is required — see --help}"
playbook="${NET_WATCH_PLAYBOOK:-playbooks/site.yml}"
tags="${NET_WATCH_ANSIBLE_TAGS:-net_watch}"
service="${NET_WATCH_SERVICE:-net-watch}"

if [[ ! -d "$NET_WATCH_IAC_DIR" ]]; then
    echo "error: NET_WATCH_IAC_DIR does not exist: $NET_WATCH_IAC_DIR" >&2
    exit 1
fi

if [[ ! -f "$NET_WATCH_IAC_DIR/$playbook" ]]; then
    echo "error: playbook not found: $NET_WATCH_IAC_DIR/$playbook" >&2
    exit 1
fi

cd "$NET_WATCH_IAC_DIR"

echo "==> Dry run (--check --diff) for tags: $tags"
ansible-playbook "$playbook" --tags "$tags" --check --diff

if [[ "$confirm" != true ]]; then
    read -r -p "Apply these changes for real? [y/N] " reply
    case "$reply" in
        [yY][eE][sS]|[yY]) ;;
        *) echo "Aborted — no changes applied."; exit 0 ;;
    esac
fi

echo "==> Applying for real"
ansible-playbook "$playbook" --tags "$tags" --diff

echo "==> Checking $service on $NET_WATCH_HOST"
ssh "$NET_WATCH_HOST" "systemctl is-active '$service' && systemctl show '$service' -p ActiveEnterTimestamp && journalctl -u '$service' -n 10 --no-pager"
