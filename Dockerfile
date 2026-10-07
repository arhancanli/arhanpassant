# ArhanPassant UCI chess engine in a container. It speaks UCI on stdin/stdout:
#
#   docker run -i --rm ghcr.io/arhancanli/arhanpassant
#
# Syzygy tables: mount them and set the option, e.g.
#   docker run -i --rm -v /path/to/syzygy:/syzygy ghcr.io/arhancanli/arhanpassant
#   setoption name SyzygyPath value /syzygy
#
# x86-64 images are built for AVX2 (x86-64-v3, any PC from about 2013 on); ARM images use NEON.
FROM rust:1-slim AS build
ARG TARGETARCH
WORKDIR /src
COPY . .
RUN if [ "$TARGETARCH" = "amd64" ]; then export RUSTFLAGS="-C target-cpu=x86-64-v3"; fi; \
    cargo build --release -p arhanpassant && ./target/release/arhanpassant bench

FROM debian:stable-slim
COPY --from=build /src/target/release/arhanpassant /usr/local/bin/arhanpassant
ENTRYPOINT ["arhanpassant"]
