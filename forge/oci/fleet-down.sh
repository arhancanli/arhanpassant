#!/bin/bash
# Terminate every fleet VM (ap-*) and its boot volume. The bucket and its data are kept.
O=~/.oci/arhanpassant/oci.sh
T=$(grep '^tenancy' ~/.oci/arhanpassant/config | cut -d= -f2)
for id in $($O compute instance list --compartment-id "$T" --all \
            --query 'data[?"lifecycle-state"!=`TERMINATED` && starts_with("display-name", `ap-`)].id' --raw-output | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin) or []))"); do
  $O compute instance terminate --instance-id "$id" --preserve-boot-volume false --force && echo "terminating $id"
done
sed -i '' '/^vm_/d' ~/.oci/arhanpassant/fleet.env 2>/dev/null
