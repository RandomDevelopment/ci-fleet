#!/usr/bin/env bash
# Generate a short-lived GitHub App installation token.
# Uses openssl (existing dependency) for JWT signing and curl for the API exchange.
# Usage:
#   github-app-token.sh --app-id ID --install-id ID --key-path PATH
#   github-app-token.sh --env-file PATH  # reads APP_ID/INSTALL_ID/KEY_PATH from env file
set -Eeuo pipefail

app_id=
install_id=
key_path=
env_file=

while (($#)); do
  case "$1" in
    --app-id) app_id=$2; shift 2 ;;
    --install-id) install_id=$2; shift 2 ;;
    --key-path) key_path=$2; shift 2 ;;
    --env-file) env_file=$2; shift 2 ;;
    --help|-h) echo "usage: $(basename "$0") --app-id ID --install-id ID --key-path PATH"; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$env_file" ]]; then
  [[ -f "$env_file" ]] || { echo "ERROR: env file not found: $env_file" >&2; exit 2; }
  while IFS='=' read -r name value; do
    case "$name" in
      CI_FLEET_GITHUB_APP_CLIENT_ID) app_id=$value ;;
      CI_FLEET_GITHUB_APP_INSTALLATION_ID) install_id=$value ;;
      CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE) key_path=$value ;;
    esac
  done < <(grep -E '^(CI_FLEET_GITHUB_APP_CLIENT_ID|CI_FLEET_GITHUB_APP_INSTALLATION_ID|CI_FLEET_GITHUB_APP_PRIVATE_KEY_FILE)=' "$env_file" || true)
fi

[[ -n "$app_id" && -n "$install_id" && -n "$key_path" ]] || { echo "ERROR: --app-id, --install-id, and --key-path are required" >&2; exit 2; }
[[ -f "$key_path" ]] || { echo "ERROR: private key file not found: $key_path" >&2; exit 2; }

# Generate JWT
now=$(date -u +%s)
exp=$((now + 540))  # 9 minutes (GitHub max is 10, leave buffer)
header='{"alg":"RS256","typ":"JWT"}'
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$now" "$exp" "$app_id")

b64url() { openssl base64 -e | tr '+/' '-_' | tr -d '=\n'; }
b64header=$(printf '%s' "$header" | b64url)
b64payload=$(printf '%s' "$payload" | b64url)
signature=$(printf '%s.%s' "$b64header" "$b64payload" | openssl dgst -sha256 -sign "$key_path" | b64url)
jwt="${b64header}.${b64payload}.${signature}"

# Exchange JWT for installation token
response=$(curl -sS -X POST \
  -H "Authorization: Bearer ${jwt}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/${install_id}/access_tokens" 2>/dev/null) || {
  echo "ERROR: token exchange request failed" >&2
  exit 2
}

token=$(printf '%s' "$response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null) || token=
if [[ -z "$token" ]]; then
  msg=$(printf '%s' "$response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null)
  echo "ERROR: token exchange rejected: ${msg:-unknown}" >&2
  exit 2
fi

printf '%s' "$token"
