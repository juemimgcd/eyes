package main

import (
	"eyes/tunnel/internal/tunnel"
	"flag"
	"log"
	"net/http"
	"time"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8090", "listen address; expose only /connect publicly")
	api := flag.String("api", "http://127.0.0.1:8000", "trusted Eyes control API")
	downloads := flag.String("downloads", "", "directory of packaged clients")
	flag.Parse()
	if _, err := tunnel.URL(*api, false); err != nil {
		log.Fatal(err)
	}
	log.Fatal((&http.Server{Addr: *listen, Handler: tunnel.Gateway(*api, *downloads), ReadHeaderTimeout: 10 * time.Second, MaxHeaderBytes: 32 << 10}).ListenAndServe())
}
