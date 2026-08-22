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
# --make-rshared only accepts a mount point. On hosts where /mnt is an
# ordinary directory on the root filesystem it must be bound to itself
# first, otherwise this fails with "not mount point or bad option".
mountpoint -q /mnt || mount --bind /mnt /mnt
mount --make-rshared /mnt

echo "Making /mnt/remote-media its own shared bind mount..."
mkdir -p /mnt/remote-media
if ! mountpoint -q /mnt/remote-media; then
    mount --bind /mnt/remote-media /mnt/remote-media
fi
mount --make-shared /mnt/remote-media

echo "Installing a systemd unit to persist mount propagation across reboots..."
cat > /etc/systemd/system/mnt-make-rshared.service <<'EOF'
[Unit]
Description=Make /mnt a shared mount point for Docker FUSE propagation
DefaultDependencies=no
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'mountpoint -q /mnt || mount --bind /mnt /mnt; mount --make-rshared /mnt; mkdir -p /mnt/remote-media; mountpoint -q /mnt/remote-media || mount --bind /mnt/remote-media /mnt/remote-media; mount --make-shared /mnt/remote-media'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnt-make-rshared.service

echo "Host setup complete."
echo "Verify with: ls -la /dev/fuse /dev/net/tun"
