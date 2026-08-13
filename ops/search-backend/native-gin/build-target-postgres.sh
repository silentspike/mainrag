#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 WORK_DIR INSTALL_PREFIX" >&2
    exit 2
fi

work_dir=$1
install_prefix=$2
lock_file=$(cd "$(dirname "$0")" && pwd)/backend.lock.json

if [[ -e "$work_dir" || -e "$install_prefix" ]]; then
    echo "WORK_DIR and INSTALL_PREFIX must not already exist" >&2
    exit 2
fi

mkdir -p "$work_dir" "$install_prefix"
postgres_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["postgresql"]["source_url"])' "$lock_file")
postgres_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["postgresql"]["source_sha256"])' "$lock_file")
vector_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema_prerequisite"]["source_url"])' "$lock_file")
vector_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["schema_prerequisite"]["source_sha256"])' "$lock_file")
flex_url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_prerequisite"]["source_url"])' "$lock_file")
flex_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_prerequisite"]["source_sha256"])' "$lock_file")

curl --fail --location --silent --show-error "$flex_url" --output "$work_dir/flex.tar.gz"
echo "$flex_sha  $work_dir/flex.tar.gz" | sha256sum --check --status
tar -xzf "$work_dir/flex.tar.gz" -C "$work_dir"
(
    cd "$work_dir/flex-2.6.4"
    ./configure --prefix="$work_dir/tools" --disable-nls --disable-shared
    make -j"$(getconf _NPROCESSORS_ONLN)"
    make install
)
export PATH="$work_dir/tools/bin:$PATH"

curl --fail --location --silent --show-error "$postgres_url" --output "$work_dir/postgresql.tar.bz2"
echo "$postgres_sha  $work_dir/postgresql.tar.bz2" | sha256sum --check --status
tar -xjf "$work_dir/postgresql.tar.bz2" -C "$work_dir"

postgres_source="$work_dir/postgresql-18.4"
configure_flags=(
    --disable-nls
    --without-icu
    --without-ldap
    --without-libxml
    --without-libxslt
    --without-lz4
    --without-pam
    --without-readline
    --without-systemd
    --without-zlib
    --without-zstd
)
(
    cd "$postgres_source"
    ./configure --prefix="$install_prefix" "${configure_flags[@]}"
    make -j"$(getconf _NPROCESSORS_ONLN)"
    make install
    make -C contrib/pg_trgm install
    make -C contrib/btree_gist install
    make -C contrib/pgcrypto install
)

curl --fail --location --silent --show-error "$vector_url" --output "$work_dir/pgvector.tar.gz"
echo "$vector_sha  $work_dir/pgvector.tar.gz" | sha256sum --check --status
mkdir "$work_dir/pgvector"
tar -xzf "$work_dir/pgvector.tar.gz" -C "$work_dir/pgvector" --strip-components=1
(
    cd "$work_dir/pgvector"
    make PG_CONFIG="$install_prefix/bin/pg_config" OPTFLAGS=""
    make PG_CONFIG="$install_prefix/bin/pg_config" install
)

"$install_prefix/bin/postgres" --version
"$install_prefix/bin/pg_config" --configure
