FROM rust:1-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/meowlaw
COPY --from=build /src/target/release/meowlaw /usr/local/bin/meowlaw
COPY ui ./ui
COPY profiles ./profiles
COPY LICENSE.md NOTICE ./
COPY meowlaw.example.toml ./meowlaw.toml
RUN sed -i 's/^bind = .*/bind = "0.0.0.0:8080"/' meowlaw.toml
VOLUME ["/srv/meowlaw/data"]
EXPOSE 8080
# Set MEOWLAW_TOKEN at runtime; without it /mcp is unauthenticated.
CMD ["meowlaw", "serve"]
