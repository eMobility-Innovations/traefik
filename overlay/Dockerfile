# Build recipe for the fleet image. Build callers MUST supply immutable image inputs;
# no registry credential or mutable production tag is embedded here.
ARG BUILD_IMAGE
ARG RUNTIME_IMAGE
ARG WEBUI_IMAGE
FROM ${WEBUI_IMAGE} AS webui
WORKDIR /src/webui
COPY webui/ ./
ENV VITE_APP_BASE_URL="" VITE_APP_BASE_API_URL="/api"
RUN corepack enable && yarn install --immutable && yarn build:prod

FROM --platform=$BUILDPLATFORM ${BUILD_IMAGE} AS build

ARG TARGETOS
ARG TARGETARCH
ARG SOURCE_COMMIT
ARG UPSTREAM_VERSION
WORKDIR /src
COPY . .
COPY --from=webui /src/webui/static ./webui/static
RUN test -s webui/static/index.html
RUN . overlay/build-inputs.lock && test "$(go env GOVERSION)" = "$GO_VERSION"
RUN test -n "$SOURCE_COMMIT" && test -n "$UPSTREAM_VERSION" \
 && CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" go build -trimpath -buildvcs=false \
      -ldflags "-s -w -X github.com/traefik/traefik/v3/pkg/version.Version=${UPSTREAM_VERSION}" \
      -o /out/traefik ./cmd/traefik

FROM ${RUNTIME_IMAGE}
ARG SOURCE_COMMIT
ARG UPSTREAM_VERSION
LABEL org.opencontainers.image.source="https://github.com/eMobility-Innovations/traefik" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.version="${UPSTREAM_VERSION}"
COPY --from=build /out/traefik /traefik
