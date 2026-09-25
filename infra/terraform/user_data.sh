#!/bin/bash
# First boot only. Installs Docker and clones the repository.
#
# Data loading and credential issuing are deliberately NOT here: loading 2M
# rows takes minutes and should be watched, and provision_logins.py prints
# passwords once, which must not end up in the cloud-init log.
set -euxo pipefail

dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

DOCKER_CONFIG=/usr/local/lib/docker
mkdir -p "$DOCKER_CONFIG/cli-plugins"
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64" \
  -o "$DOCKER_CONFIG/cli-plugins/docker-compose"
chmod +x "$DOCKER_CONFIG/cli-plugins/docker-compose"

sudo -u ec2-user git clone ${repo_url} /home/ec2-user/pharma-analytics-copilot || true

echo "first boot complete: docker and the repository are ready" > /var/log/pac-bootstrap.done
