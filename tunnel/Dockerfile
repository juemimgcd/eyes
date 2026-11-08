FROM golang:1.27.3-alpine AS build
WORKDIR /src
COPY tunnel/go.mod tunnel/go.sum ./
RUN go mod download
COPY tunnel/ ./
RUN CGO_ENABLED=0 go vet ./... && CGO_ENABLED=0 sh package.sh /out
FROM alpine:3.23
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 eyes
COPY --from=build /out /app
USER eyes
ENTRYPOINT ["/app/eyes-gateway"]
CMD ["--listen", "0.0.0.0:8090", "--api", "http://api:8000", "--downloads", "/app/downloads"]
