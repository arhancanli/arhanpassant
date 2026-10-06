#!/bin/bash
# First-boot setup for an ArhanPassant fleet VM (Ubuntu 24.04, ARM or x86).
# Installs a Rust toolchain, then runs vm-node.sh as a systemd service: it
# builds the engine from the public repository and generates self-play data
# on every core, uploading finished chunks to the fleet bucket.
set -ux
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y build-essential git python3 curl zstd
sudo -u ubuntu bash -lc 'curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable'
install -o ubuntu -m 600 /dev/null /home/ubuntu/par.txt
echo '__PAR__' > /home/ubuntu/par.txt
curl -fsSL https://raw.githubusercontent.com/arhancanli/arhanpassant/__BRANCH__/forge/oci/vm-node.sh -o /home/ubuntu/vm-node.sh
chown ubuntu:ubuntu /home/ubuntu/vm-node.sh && chmod +x /home/ubuntu/vm-node.sh
cat > /etc/systemd/system/ap-node.service <<'UNIT'
[Unit]
Description=ArhanPassant fleet node
After=network-online.target
[Service]
User=ubuntu
Environment=BRANCH=__BRANCH__
ExecStart=/home/ubuntu/vm-node.sh
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ap-node.service
