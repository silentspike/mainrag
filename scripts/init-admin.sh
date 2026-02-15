#!/usr/bin/env bash
# init-admin.sh — Create initial admin user with random password
# Sprint 2.7: Replaces insecure default admin INSERT in schema_security.sql
set -euo pipefail

DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-mainrag}"
DB_USER="${POSTGRES_USER:-mainrag}"
DB_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"

# Generate a strong random password (20 chars, meets policy: upper+lower+digit+special)
ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 18)A1!"

# Hash password with bcrypt (cost 12) via Python
HASH=$(python3 -c "
import bcrypt
h = bcrypt.hashpw(b'${ADMIN_PASSWORD}', bcrypt.gensalt(rounds=12))
print(h.decode())
" 2>/dev/null || {
    # Fallback: use htpasswd if python3+bcrypt not available
    echo "ERROR: python3 with bcrypt module required. Install: pip install bcrypt" >&2
    exit 1
})

# Insert admin user (skip if already exists)
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -q <<SQL
INSERT INTO users (username, email, password_hash, display_name, is_active, is_verified, is_admin)
VALUES ('admin', 'admin@localhost', '$HASH', 'Administrator', TRUE, TRUE, TRUE)
ON CONFLICT (username) DO NOTHING;

-- Assign admin role if roles table exists
DO \$\$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'roles') THEN
    INSERT INTO user_roles (user_id, role_id)
    SELECT u.id, r.id FROM users u, roles r
    WHERE u.username = 'admin' AND r.name = 'admin'
    ON CONFLICT DO NOTHING;
  END IF;
END \$\$;
SQL

echo "========================================"
echo "Admin user created successfully!"
echo "Username: admin"
echo "Password: ${ADMIN_PASSWORD}"
echo ""
echo "SAVE THIS PASSWORD — it will not be shown again."
echo "Change it immediately: POST /api/v1/auth/change-password"
echo "========================================"
