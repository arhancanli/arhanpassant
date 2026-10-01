#!/bin/bash
# Stop all fleet compute (deletes the fleet resource group). Stored data in the
# forge storage account is kept; download it with pull.sh before the credit ends.
set -euo pipefail
az group delete -n "${FLEET_RG:-arhanpassant-fleet}" --yes --no-wait
echo "Fleet deletion requested."
