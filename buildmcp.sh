#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

MCP_ROOT="mcp-servers"
BUILD_ROOT=".mcp-build"

MCP_DIRS=(
    "$MCP_ROOT/slack-mcp"
)

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"

git submodule update --init --recursive

build_node_mcp() {
    local mcp_dir="$1"
    local artifact_dir="$2"

    npm --prefix "$mcp_dir" install \
        --include=dev \
        --ignore-scripts \
        --no-audit \
        --no-fund

    npm --prefix "$mcp_dir" run build
    npm --prefix "$mcp_dir" prune --omit=dev --ignore-scripts

    mkdir -p "$artifact_dir"

    cp "$mcp_dir/package.json" "$artifact_dir/package.json"
    cp "$mcp_dir/package-lock.json" "$artifact_dir/package-lock.json"
    cp -a "$mcp_dir/node_modules" "$artifact_dir/node_modules"
    cp -a "$mcp_dir/dist" "$artifact_dir/dist"
}

# Python MCP servers ship a relocatable dependency tree: a venv's scripts embed the
# build-time interpreter path, so the artifact would break once copied into an image.
# Run it with the consuming image's interpreter and PYTHONPATH=<artifact>/site-packages.
build_python_mcp() {
    local mcp_dir="$1"
    local artifact_dir="$2"

    mkdir -p "$artifact_dir/site-packages"

    python3 -m pip install \
        --no-cache-dir \
        --target "$artifact_dir/site-packages" \
        "$mcp_dir"
}

for mcp_dir in "${MCP_DIRS[@]}"; do
    mcp_name="$(basename "$mcp_dir")"
    artifact_dir="$BUILD_ROOT/$mcp_name"

    echo "Building $mcp_name"

    if [ -f "$mcp_dir/pyproject.toml" ]; then
        build_python_mcp "$mcp_dir" "$artifact_dir"
    else
        build_node_mcp "$mcp_dir" "$artifact_dir"
    fi
done
