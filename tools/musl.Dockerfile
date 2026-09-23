# Build image for the static (musl) release binaries — used by tools/mkreleases.py,
# which builds it on first use as ircbuild-musl:static.  Static OpenSSL + libcurl
# (and curl's own static deps) so the C daemons carry no shared-library needs.
FROM alpine:latest
RUN apk add --no-cache build-base bash pkgconf linux-headers \
      openssl-dev openssl-libs-static \
      curl-dev curl-static nghttp2-static nghttp3-static zlib-static zstd-static brotli-static \
      libidn2-static libpsl-static libunistring-static
