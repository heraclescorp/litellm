#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

MCP_ROOT="mcp-servers"
BUILD_ROOT=".mcp-build"
MCP_PYTHON_VERSION="${MCP_PYTHON_VERSION:-3.13}"
MCP_PYTHON_TARGETS=(
    "amd64:x86_64-manylinux_2_17"
    "arm64:aarch64-manylinux_2_17"
)

MCP_DIRS=(
    "$MCP_ROOT/slack-mcp"
    "$MCP_ROOT/figma-mcp"
    "$MCP_ROOT/bloomberg-mcp"
)

mkdir -p "$BUILD_ROOT"

git submodule update --init --recursive

build_node_mcp() {
    local mcp_dir="$1"
    local artifact_dir="$2"

    local work_dir="$BUILD_ROOT/.$(basename "$mcp_dir")-build"

    rm -rf "$work_dir"
    mkdir -p "$work_dir" "$artifact_dir"
    cp "$mcp_dir"/{package.json,pnpm-lock.yaml,tsconfig.json,tsup.config.ts} "$work_dir/"
    cp -a "$mcp_dir/src" "$work_dir/src"

    (
        cd "$work_dir"
        export CI=true
        pnpm_config=(
            --config.node-linker=isolated
            --config.modules-dir=node_modules
            --config.virtual-store-dir=node_modules/.pnpm
        )
        corepack pnpm install "${pnpm_config[@]}" --frozen-lockfile --ignore-scripts --prod=false
        corepack pnpm "${pnpm_config[@]}" run build
        rm -rf node_modules
        corepack pnpm install "${pnpm_config[@]}" --frozen-lockfile --ignore-scripts --prod
    )

    cp "$work_dir/package.json" "$artifact_dir/package.json"
    cp -a "$work_dir/node_modules" "$artifact_dir/node_modules"
    cp -a "$work_dir/dist" "$artifact_dir/dist"
    rm -rf "$work_dir"
}

# Python MCP servers ship a relocatable dependency tree: a venv's scripts embed the
# build-time interpreter path, so the artifact would break once copied into an image.
# Run it with the consuming image's interpreter and PYTHONPATH=<artifact>/site-packages.
build_python_mcp() {
    local mcp_dir="$1"
    local artifact_dir="$2"
    local requirements_file="$BUILD_ROOT/$(basename "$mcp_dir")-requirements.txt"

    uv export \
        --project "$mcp_dir" \
        --frozen \
        --no-dev \
        --quiet \
        --no-emit-project \
        --output-file "$requirements_file"

    for python_target in "${MCP_PYTHON_TARGETS[@]}"; do
        local target_arch="${python_target%%:*}"
        local target_platform="${python_target#*:}"
        local site_packages="$artifact_dir/$target_arch/site-packages"

        mkdir -p "$site_packages"
        rm -rf "$mcp_dir/build"

        uv pip install \
            --python-version "$MCP_PYTHON_VERSION" \
            --python-platform "$target_platform" \
            --no-cache-dir \
            --target "$site_packages" \
            --requirements "$requirements_file"
        uv pip install \
            --python-version "$MCP_PYTHON_VERSION" \
            --python-platform "$target_platform" \
            --no-cache-dir \
            --no-deps \
            --target "$site_packages" \
            "$mcp_dir"
    done

    rm "$requirements_file"
}

for mcp_dir in "${MCP_DIRS[@]}"; do
    mcp_name="$(basename "$mcp_dir")"
    artifact_dir="$BUILD_ROOT/$mcp_name"
    rm -rf "$artifact_dir"

    echo "Building $mcp_name"

    if [ -f "$mcp_dir/pyproject.toml" ]; then
        build_python_mcp "$mcp_dir" "$artifact_dir"
    else
        build_node_mcp "$mcp_dir" "$artifact_dir"
    fi
done
