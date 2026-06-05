#!/bin/bash
# Create Duplicati backup jobs via API
# Encryption passphrase: iJqb6FTaoWEp1a4KelbpMtTbgQrCfELt  ← save this to Vaultwarden

DUPLICATI="http://localhost:8200"
WEBPASSWORD="changeme"
PASSPHRASE="iJqb6FTaoWEp1a4KelbpMtTbgQrCfELt"

# Get access token
TOKEN=$(curl -s -X POST "$DUPLICATI/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"Password\":\"$WEBPASSWORD\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['AccessToken'])")

if [ -z "$TOKEN" ]; then
  echo "Failed to get auth token"
  exit 1
fi

AUTH="Authorization: Bearer $TOKEN"

create_backup() {
  local name="$1" target="$2" sources_json="$3" hour="$4"
  echo "Creating: $name"
  result=$(curl -s -X POST "$DUPLICATI/api/v1/backups" \
    -H "$AUTH" \
    -H "Content-Type: application/json" \
    -d "{
      \"Backup\": {
        \"Name\": \"$name\",
        \"TargetURL\": \"file://$target\",
        \"DBPath\": \"@\",
        \"Sources\": $sources_json,
        \"Settings\": [
          {\"Name\": \"encryption-module\", \"Value\": \"aes\"},
          {\"Name\": \"compression-module\", \"Value\": \"zip\"},
          {\"Name\": \"passphrase\", \"Value\": \"$PASSPHRASE\"},
          {\"Name\": \"retention-policy\", \"Value\": \"1W:1D,4W:1W,12M:1M\"}
        ],
        \"Filters\": [],
        \"Metadata\": {}
      },
      \"Schedule\": {
        \"Time\": \"2026-01-01T0${hour}:00:00Z\",
        \"Repeat\": \"1D\",
        \"AllowedDays\": null,
        \"Tags\": []
      }
    }")
  echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ Created, ID:', d.get('ID', d))" 2>/dev/null || echo "  Response: $result"
}

create_backup "Appdata Daily" "/backups/appdata" '[ "/source/appdata" ]' "3"
create_backup "Config Daily"  "/backups/config"  '[ "/source/compose", "/source/npm" ]' "3:15"

echo ""
echo "Passphrase (save to Vaultwarden): iJqb6FTaoWEp1a4KelbpMtTbgQrCfELt"
