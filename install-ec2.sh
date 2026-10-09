#!/bin/bash
# =====================================================================
# NordEvent Lab - EC2 paigaldus (Amazon Linux 2023)
# Kasutus: kleebi kogu fail EC2 loomisel väljale
#   Advanced details -> User data
# või käivita käsitsi: sudo bash install-ec2.sh
# =====================================================================
# Koodi asukoht (avalik GitHubi repositoorium):
LAB_URL="https://github.com/heino1/nordevent-lab/archive/refs/heads/main.tar.gz"

exec > >(tee -a /var/log/nordevent-install.log) 2>&1
set -e
echo "== NordEvent Lab paigaldus algas $(date)"
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
curl -fsSL https://github.com/docker/buildx/releases/download/v0.17.1/buildx-v0.17.1.linux-amd64 \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/*
cd /home/ec2-user
mkdir -p nordevent-lab
curl -fsSL "$LAB_URL" | tar xz -C nordevent-lab --strip-components=1
chown -R ec2-user:ec2-user nordevent-lab
chmod +x nordevent-lab/lab.sh
cd nordevent-lab
sudo -u ec2-user cp scenarios/00-baas.env .env
sudo -u ec2-user ./lab.sh up
echo "NordEvent Lab on valmis: $(date). Ava brauseris http://<avalik IP>/" > /home/ec2-user/VALMIS.txt
echo "== Paigaldus lõppes $(date)"
