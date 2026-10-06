#!/bin/bash
# Fleet VMs with state, shape, cores and public IP; IPs are cached in ~/.oci/arhanpassant/ips.txt.
O=~/.oci/arhanpassant/oci.sh
ST=~/.oci/arhanpassant
T=$(grep '^tenancy' $ST/config | cut -d= -f2)
: > $ST/ips.txt
$O compute instance list --compartment-id "$T" --all \
   --query 'data[?"lifecycle-state"!=`TERMINATED` && starts_with("display-name", `ap-`)].[id,"display-name",shape,"lifecycle-state","shape-config".ocpus]' \
   --raw-output 2>/dev/null | python3 -c "
import json, sys, subprocess, os
rows = json.load(sys.stdin) or []
for vid, name, shape, st, ocpus in rows:
    ip = ''
    if st == 'RUNNING':
        out = subprocess.run([os.path.expanduser('~/.oci/arhanpassant/oci.sh'), 'compute', 'instance', 'list-vnics', '--instance-id', vid,
                              '--query', 'data[0].\"public-ip\"', '--raw-output'], capture_output=True, text=True).stdout.strip()
        ip = out
        with open(os.path.expanduser('~/.oci/arhanpassant/ips.txt'), 'a') as f:
            f.write(f'{name} {ip}\n')
    print(f'{name:8} {shape:22} {ocpus:>5} {st:12} {ip}')
"
