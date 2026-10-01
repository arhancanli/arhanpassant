"""Write the cloud-init file for a forge node (reads ~/.arhanpassant/forge.env)."""
import os
import sys

env = open(os.path.expanduser("~/.arhanpassant/forge.env")).read()
indent = lambda text, n: "".join(" " * n + line + "\n" for line in text.splitlines())
bootstrap = """#!/bin/bash
. /etc/arhanpassant.env
curl -fsS --retry 10 --retry-delay 10 "$BASE/bootstrap/node.sh?$SAS" -o /usr/local/bin/arhanpassant-node
chmod +x /usr/local/bin/arhanpassant-node
systemctl daemon-reload
systemctl enable --now arhanpassant-node
"""
unit = """[Unit]
Description=ArhanPassant self-play node
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/arhanpassant-node
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
doc = "#cloud-config\npackage_update: true\npackages: [build-essential, curl]\nwrite_files:\n"
doc += "  - path: /etc/arhanpassant.env\n    permissions: '0600'\n    content: |\n" + indent(env, 6)
doc += "  - path: /usr/local/sbin/ap-bootstrap.sh\n    permissions: '0700'\n    content: |\n" + indent(bootstrap, 6)
doc += "  - path: /etc/systemd/system/arhanpassant-node.service\n    content: |\n" + indent(unit, 6)
doc += "runcmd:\n  - [bash, /usr/local/sbin/ap-bootstrap.sh]\n"
with open(sys.argv[1], "w") as f:
    f.write(doc)
