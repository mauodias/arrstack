#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (sudo bash setup-host.sh)." >&2
    exit 1
fi

echo "Loading fuse and tun kernel modules..."
modprobe fuse
modprobe tun

echo "Making /mnt a shared mount point..."
mount --make-rshared /mnt

echo "Installing a systemd unit to persist mount propagation across reboots..."
cat > /etc/systemd/system/mnt-make-rshared.service <<'EOF'
[Unit]
Description=Make /mnt a shared mount point for Docker FUSE propagation
DefaultDependencies=no
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/bin/mount --make-rshared /mnt
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnt-make-rshared.service

echo "Host setup complete."
echo "Verify with: ls -la /dev/fuse /dev/net/tun"
