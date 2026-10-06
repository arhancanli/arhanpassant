#!/bin/bash
# Start the ArhanPassant fleet on Oracle Cloud (me-dubai-1): a VCN with one
# public subnet (SSH only from this machine's IPv4), a storage bucket with a
# read/write pre-authenticated link for self-play uploads, and one VM per CPU
# family sized to that family's core limit. Every VM builds the engine from
# GitHub and generates self-play data on all cores (see vm-node.sh).
#
#   bash forge/oci/fleet-up.sh            # everything
#   ONLY="a1 e4" bash forge/oci/fleet-up.sh
#
# Session: ~/.oci/arhanpassant/config (oci session authenticate / refresh).
set -uo pipefail
cd "$(dirname "$0")/../.."
O=~/.oci/arhanpassant/oci.sh
ST=~/.oci/arhanpassant
T=$(grep '^tenancy' $ST/config | cut -d= -f2)
BRANCH=${BRANCH:-engine/elo-20261006}
AD=$($O iam availability-domain list --compartment-id "$T" --query 'data[0].name' --raw-output)
MY4=$(curl -4 -s --max-time 10 https://ifconfig.me)
echo "tenancy $T, AD $AD, SSH from $MY4"

state() { grep "^$1=" $ST/fleet.env 2>/dev/null | tail -1 | cut -d= -f2-; }
save() { echo "$1=$2" >> $ST/fleet.env; }

# --- network ---------------------------------------------------------------
VCN=$(state vcn)
if [ -z "$VCN" ]; then
  VCN=$($O network vcn create --compartment-id "$T" --cidr-blocks '["10.42.0.0/16"]' --display-name arhanpassant-vcn \
        --dns-label apvcn --wait-for-state AVAILABLE --query data.id --raw-output) || exit 1
  save vcn "$VCN"
fi
IGW=$(state igw)
if [ -z "$IGW" ]; then
  IGW=$($O network internet-gateway create --compartment-id "$T" --vcn-id "$VCN" --is-enabled true --display-name ap-igw \
        --wait-for-state AVAILABLE --query data.id --raw-output) || exit 1
  save igw "$IGW"
fi
RT=$($O network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)
$O network route-table update --rt-id "$RT" --force \
  --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" > /dev/null || exit 1
SL=$($O network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)
$O network security-list update --security-list-id "$SL" --force \
  --ingress-security-rules "[{\"source\":\"$MY4/32\",\"protocol\":\"6\",\"tcpOptions\":{\"destinationPortRange\":{\"min\":22,\"max\":22}}}]" \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all"}]' > /dev/null || exit 1
SUBNET=$(state subnet)
if [ -z "$SUBNET" ]; then
  SUBNET=$($O network subnet create --compartment-id "$T" --vcn-id "$VCN" --cidr-block 10.42.1.0/24 --display-name ap-public \
           --dns-label appub --wait-for-state AVAILABLE --query data.id --raw-output) || exit 1
  save subnet "$SUBNET"
fi
echo "network ready: subnet $SUBNET"

# --- bucket and upload link ---------------------------------------------------
NS=$($O os ns get --query data --raw-output)
save namespace "$NS"
$O os bucket get --namespace "$NS" --bucket-name arhanpassant-fleet > /dev/null 2>&1 || \
  $O os bucket create --compartment-id "$T" --namespace "$NS" --name arhanpassant-fleet > /dev/null || exit 1
if [ ! -s $ST/par.txt ]; then
  $O os preauth-request create --namespace "$NS" --bucket-name arhanpassant-fleet --name fleet-rw \
     --access-type AnyObjectReadWrite --bucket-listing-action ListObjects --time-expires 2026-11-15T00:00:00Z \
     --query 'data."full-path"' --raw-output > $ST/par.txt || exit 1
  chmod 600 $ST/par.txt
fi
echo "bucket arhanpassant-fleet ready (upload link in $ST/par.txt)"

# --- VMs ----------------------------------------------------------------------
ARM_IMG=$($O compute image list --compartment-id "$T" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
          --shape VM.Standard.A1.Flex --sort-by TIMECREATED --limit 1 --query 'data[0].id' --raw-output)
X86_IMG=$($O compute image list --compartment-id "$T" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
          --shape VM.Standard.E4.Flex --sort-by TIMECREATED --limit 1 --query 'data[0].id' --raw-output)
INIT=$(mktemp)
sed -e "s#__PAR__#$(cat $ST/par.txt)#" -e "s#__BRANCH__#$BRANCH#g" forge/oci/vm-init.sh > "$INIT"

# name shape ocpus memory_gb image (ocpus empty = fixed shape)
FLEET=(
  "a1 VM.Standard.A1.Flex 16 32 $ARM_IMG"
  "a2 VM.Standard.A2.Flex 29 58 $ARM_IMG"
  "a4 VM.Standard.A4.Flex 6 12 $ARM_IMG"
  "e5 VM.Standard.E5.Flex 13 26 $X86_IMG"
  "e4 VM.Standard.E4.Flex 16 32 $X86_IMG"
  "e3 VM.Standard.E3.Flex 16 32 $X86_IMG"
  "s3 VM.Standard3.Flex 10 20 $X86_IMG"
  "e2a VM.Standard.E2.8 - - $X86_IMG"
  "e2b VM.Standard.E2.4 - - $X86_IMG"
  "e2c VM.Standard.E2.1 - - $X86_IMG"
  "s2a VM.Standard2.4 - - $X86_IMG"
  "s2b VM.Standard2.2 - - $X86_IMG"
)
for spec in "${FLEET[@]}"; do
  read -r name shape ocpus mem img <<< "$spec"
  if [ -n "${ONLY:-}" ] && [[ " $ONLY " != *" $name "* ]]; then continue; fi
  if [ -n "$(state "vm_$name")" ]; then echo "ap-$name already requested"; continue; fi
  cfg=()
  [ "$ocpus" != "-" ] && cfg=(--shape-config "{\"ocpus\":$ocpus,\"memoryInGBs\":$mem}")
  id=$($O compute instance launch --compartment-id "$T" --availability-domain "$AD" --display-name "ap-$name" \
        --shape "$shape" "${cfg[@]}" --image-id "$img" --subnet-id "$SUBNET" --assign-public-ip true \
        --ssh-authorized-keys-file ~/.ssh/arhanpassant_oci.pub --user-data-file "$INIT" \
        --boot-volume-size-in-gbs 100 --query data.id --raw-output 2> $ST/launch-$name.err)
  if [ -n "$id" ]; then
    save "vm_$name" "$id"; echo "requested ap-$name ($shape ${ocpus/-/fixed})"
  else
    echo "FAILED ap-$name ($shape): $(grep -oE '"message": "[^"]*"' $ST/launch-$name.err | head -1)"
  fi
done
rm -f "$INIT"
echo "Fleet requested. Status: bash forge/oci/fleet-status.sh"
