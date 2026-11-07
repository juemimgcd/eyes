package main

import (
	"context"
	"eyes/tunnel/internal/tunnel"
	"flag"
	"log"
	"os"
	"os/signal"
)

func main() {
	var c tunnel.ClientConfig
	flag.StringVar(&c.Server, "server", "", "Eyes API URL")
	flag.StringVar(&c.Gateway, "gateway", "", "public tunnel URL; defaults to API sibling /tunnel")
	flag.StringVar(&c.PairCode, "pair-code", "", "short-lived pairing code")
	flag.StringVar(&c.Local, "local", "", "fixed local Agent endpoint")
	flag.StringVar(&c.Protocol, "protocol", "json", "json or chat_completions")
	flag.StringVar(&c.AuthEnv, "auth-env", "", "environment variable containing full Agent Authorization")
	flag.StringVar(&c.ModelUpstream, "model-upstream", "", "original model API base URL, enables observation")
	flag.StringVar(&c.ModelListen, "model-listen", "127.0.0.1:8787", "loopback model proxy address")
	flag.StringVar(&c.OutputPointer, "output-pointer", "", "JSON Pointer selecting an output object")
	dir := flag.String("state-dir", ".eyes/tunnel", "private connection and observation spool directory")
	flag.Parse()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if err := tunnel.RunClient(ctx, *dir, c); err != nil {
		log.Fatal(err)
	}
}
