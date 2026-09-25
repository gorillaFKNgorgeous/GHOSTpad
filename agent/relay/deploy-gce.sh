#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Deploy the single-owner GhostBlender relay to a small persistent GCE VM.
# Intended for Google Cloud Shell. Secrets are generated on the VM and are never
# written to this repository or echoed by this script.

set -Eeuo pipefail

VM="${GHOSTBLENDER_VM:-ghostblender-relay}"
ADDRESS="${GHOSTBLENDER_ADDRESS:-ghostblender-relay-ip}"
REPO="https://github.com/gorillaFKNgorgeous/GHOSTpad.git"
LEGACY_REDIRECT="https://chatgpt.com/connector_platform_oauth_redirect"

PROJECT="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "No Google Cloud project is selected." >&2
  echo "Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

gcloud services enable compute.googleapis.com iap.googleapis.com --project "$PROJECT" >/dev/null

# Prefer the location/network of an existing relay VM. Earlier versions defaulted
# to us-west1-b, which can accidentally try to create a second subnet whose CIDR
# overlaps the already-running relay network in another region.
ZONE="${GHOSTBLENDER_ZONE:-}"
if [[ -z "$ZONE" ]]; then
  mapfile -t relay_zones < <(
    gcloud compute instances list \
      --project "$PROJECT" \
      --filter="name=$VM" \
      --format='value(zone.basename())'
  )
  if (( ${#relay_zones[@]} > 1 )); then
    echo "More than one VM named $VM exists. Set GHOSTBLENDER_ZONE explicitly." >&2
    printf '  %s\n' "${relay_zones[@]}" >&2
    exit 1
  elif (( ${#relay_zones[@]} == 1 )); then
    ZONE="${relay_zones[0]}"
  else
    ZONE="us-west1-b"
  fi
fi
REGION="${ZONE%-*}"

instance_exists=0
if gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" >/dev/null 2>&1; then
  instance_exists=1
fi

if [[ "$instance_exists" == 1 ]]; then
  vm_network_url="$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" \
    --format='get(networkInterfaces[0].network)')"
  vm_subnet_url="$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" \
    --format='get(networkInterfaces[0].subnetwork)')"
  NETWORK="${GHOSTBLENDER_NETWORK:-${vm_network_url##*/}}"
  SUBNET="${GHOSTBLENDER_SUBNET:-${vm_subnet_url##*/}}"
else
  NETWORK="${GHOSTBLENDER_NETWORK:-ghostblender-net}"
  SUBNET="${GHOSTBLENDER_SUBNET:-ghostblender-relay-${REGION}}"
fi

echo "Project: $PROJECT"
echo "Zone:    $ZONE"
echo "Region:  $REGION"
echo "VM:      $VM"
echo "Network: $NETWORK"
echo "Subnet:  $SUBNET"
echo "Address: $ADDRESS"

if [[ "$instance_exists" == 0 ]]; then
  if ! gcloud compute networks describe "$NETWORK" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud compute networks create "$NETWORK" --project "$PROJECT" --subnet-mode=custom
  fi

  if ! gcloud compute networks subnets describe "$SUBNET" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
    # Pick the historical relay CIDR only when it is actually unused. If another
    # subnet already owns it, choose a distinct private /24 rather than colliding.
    subnet_range="10.42.0.0/24"
    if gcloud compute networks subnets list --project "$PROJECT" \
        --network "$NETWORK" --format='value(ipCidrRange)' | grep -Fx "$subnet_range" >/dev/null; then
      subnet_range="10.43.0.0/24"
    fi
    gcloud compute networks subnets create "$SUBNET" \
      --project "$PROJECT" \
      --region "$REGION" \
      --network "$NETWORK" \
      --range "$subnet_range"
  fi
fi

if ! gcloud compute firewall-rules describe ghostblender-relay-https --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute firewall-rules create ghostblender-relay-https \
    --project "$PROJECT" \
    --network "$NETWORK" \
    --allow tcp:80,tcp:443 \
    --target-tags ghostblender-relay
fi

# Administration is tunneled through Google IAP. Do not expose SSH to 0.0.0.0/0.
if ! gcloud compute firewall-rules describe ghostblender-relay-iap-ssh --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute firewall-rules create ghostblender-relay-iap-ssh \
    --project "$PROJECT" \
    --network "$NETWORK" \
    --direction INGRESS \
    --allow tcp:22 \
    --source-ranges 35.235.240.0/20 \
    --target-tags ghostblender-relay
fi

# The relay origin is part of OAuth metadata and the device pairing. Keep it
# stable across VM stops/restarts by reserving a regional static external IPv4.
if ! gcloud compute addresses describe "$ADDRESS" --project "$PROJECT" --region "$REGION" >/dev/null 2>&1; then
  if [[ "$instance_exists" == 1 ]]; then
    existing_ip="$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" \
      --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
    if [[ -z "$existing_ip" ]]; then
      echo "Existing VM has no external IPv4 address to promote." >&2
      exit 1
    fi
    gcloud compute addresses create "$ADDRESS" \
      --project "$PROJECT" \
      --region "$REGION" \
      --addresses "$existing_ip"
  else
    gcloud compute addresses create "$ADDRESS" \
      --project "$PROJECT" \
      --region "$REGION"
  fi
fi

STATIC_IP="$(gcloud compute addresses describe "$ADDRESS" --project "$PROJECT" --region "$REGION" --format='get(address)')"
if [[ -z "$STATIC_IP" ]]; then
  echo "Static external IPv4 reservation has no address." >&2
  exit 1
fi

if [[ "$instance_exists" == 0 ]]; then
  startup_script="$(mktemp)"
  cat >"$startup_script" <<'STARTUP'
#!/usr/bin/env bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose git ca-certificates curl
systemctl enable --now docker
touch /var/lib/ghostblender-relay-ready
STARTUP

  gcloud compute instances create "$VM" \
    --project "$PROJECT" \
    --zone "$ZONE" \
    --machine-type e2-micro \
    --network-interface "subnet=$SUBNET,address=$STATIC_IP" \
    --image-family debian-12 \
    --image-project debian-cloud \
    --boot-disk-type pd-standard \
    --boot-disk-size 10GB \
    --tags ghostblender-relay \
    --metadata-from-file startup-script="$startup_script"
  rm -f "$startup_script"
else
  state="$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" --format='get(status)')"
  if [[ "$state" != "RUNNING" ]]; then
    gcloud compute instances start "$VM" --project "$PROJECT" --zone "$ZONE"
  fi
fi

# Wait for SSH through IAP and the startup package install.
for _ in {1..60}; do
  if gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --quiet --tunnel-through-iap \
      --command 'test -f /var/lib/ghostblender-relay-ready && command -v docker-compose >/dev/null' \
      >/dev/null 2>&1; then
    break
  fi
  sleep 5
done

gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --quiet --tunnel-through-iap \
  --command 'test -f /var/lib/ghostblender-relay-ready && command -v docker-compose >/dev/null'

IP="$(gcloud compute instances describe "$VM" --project "$PROJECT" --zone "$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
if [[ -z "$IP" ]]; then
  echo "The VM has no external IPv4 address." >&2
  exit 1
fi
if [[ "$IP" != "$STATIC_IP" ]]; then
  echo "VM external IP $IP does not match reserved relay IP $STATIC_IP." >&2
  echo "Refusing to change the public origin automatically. Attach the reserved address deliberately, then rerun." >&2
  exit 1
fi

# sslip.io maps an IP embedded in a hostname back to that IP, avoiding the need
# to purchase a domain for the initial acceptance test. Because the IP is now
# reserved, this HTTPS origin remains stable across ordinary VM stops/restarts.
HOST="${IP//./-}.sslip.io"
ORIGIN="https://${HOST}"

echo "Relay origin: $ORIGIN"

remote_script="$(mktemp)"
cat >"$remote_script" <<REMOTE
set -Eeuo pipefail
if [[ ! -d "\$HOME/ghostblender/.git" ]]; then
  git clone --depth 1 "$REPO" "\$HOME/ghostblender"
else
  git -C "\$HOME/ghostblender" remote set-url origin "$REPO"
  git -C "\$HOME/ghostblender" fetch --depth 1 origin main
  git -C "\$HOME/ghostblender" checkout main
  git -C "\$HOME/ghostblender" reset --hard origin/main
fi
cd "\$HOME/ghostblender/agent/relay"
if [[ ! -f .env ]]; then
  python3 configure.py \
    --origin "$ORIGIN" \
    --redirect-uri "$LEGACY_REDIRECT"
else
  existing_origin="\$(sed -n 's/^PUBLIC_ORIGIN=//p' .env)"
  if [[ "\$existing_origin" != "$ORIGIN" ]]; then
    echo "Existing relay credentials belong to \$existing_origin, but this VM now resolves to $ORIGIN." >&2
    echo "Do not silently replace credentials. Update the origin/redirect allowlist deliberately." >&2
    exit 1
  fi
fi
sudo docker-compose up -d --build
REMOTE

gcloud compute scp "$remote_script" "$VM:/tmp/ghostblender-deploy.sh" \
  --project "$PROJECT" --zone "$ZONE" --quiet --tunnel-through-iap >/dev/null
rm -f "$remote_script"
gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --quiet --tunnel-through-iap \
  --command 'bash /tmp/ghostblender-deploy.sh && rm -f /tmp/ghostblender-deploy.sh'

# Caddy needs a short window to complete ACME certificate issuance.
healthy=0
for _ in {1..36}; do
  if curl -fsS --max-time 5 "$ORIGIN/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done
if [[ "$healthy" != 1 ]]; then
  echo "Relay containers started, but HTTPS health did not become ready." >&2
  echo "Inspect with:" >&2
  echo "  gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap --command 'cd ~/ghostblender/agent/relay && sudo docker-compose logs --tail=120'" >&2
  exit 1
fi

echo
echo "GhostBlender relay is live: $ORIGIN"
echo "Health: $ORIGIN/health"
echo
echo "To reveal the one-time values you must enter into GhostBlender / ChatGPT, run:"
echo "  gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap --command \"cd ~/ghostblender/agent/relay && grep -E '^(PUBLIC_ORIGIN|DEVICE_ID|DEVICE_TOKEN|OAUTH_CLIENT_ID|OAUTH_CLIENT_SECRET|OWNER_KEY)=' .env\""
echo
echo "Do not paste those secret values into GitHub or this chat."
echo "For a NEW ChatGPT developer-mode app, ChatGPT will display the exact redirect URI in app management."
echo "The relay is initially seeded with the stable issuer-aware callback:"
echo "  $LEGACY_REDIRECT"
echo "If ChatGPT displays a different callback, add that exact URI to OAUTH_REDIRECT_URIS in .env and restart docker-compose before authorizing."
echo
echo "One-time embedded AI sign-in (uses your Codex/ChatGPT account, not an API key):"
echo "  gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap --command 'cd ~/ghostblender/agent/relay && sudo docker-compose exec relay python chat_login.py'"
