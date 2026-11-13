#!/bin/sh
# Run from tunnel/. Requires Go and sha256sum; artifacts stay out of source control.
set -eu
output=${1:-../dist/tunnel}
mkdir -p "$output/downloads"
go build -trimpath -ldflags='-s -w' -o "$output/eyes-gateway" ./cmd/eyes-gateway
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags='-s -w' -o "$output/downloads/eyes-tunnel-darwin-arm64" ./cmd/eyes-tunnel
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o "$output/downloads/eyes-tunnel-linux-amd64" ./cmd/eyes-tunnel
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o "$output/downloads/eyes-tunnel-windows-amd64.exe" ./cmd/eyes-tunnel
# Ship license notices with binaries, including transitive module notices.
notice="$output/downloads/THIRD_PARTY_LICENSES.txt"
printf 'Eyes tunnel dependency licenses\n' > "$notice"
go list -m -f '{{.Dir}}' all | while IFS= read -r module; do
    for license in "$module"/LICENSE* "$module"/COPYING* "$module"/NOTICE*; do
        if [ -f "$license" ]; then
            printf '\n%s\n' "$license" >> "$notice"
            cat "$license" >> "$notice"
        fi
    done
done
(cd "$output/downloads" && sha256sum eyes-tunnel-* THIRD_PARTY_LICENSES.txt > SHA256SUMS)
