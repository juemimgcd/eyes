package tunnel

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/rancher/remotedialer"
)

type ClientConfig struct {
	Server           string `json:"server"`
	Gateway          string `json:"gateway"`
	Local            string `json:"local"`
	Token            string `json:"token"`
	ObservationToken string `json:"observation_token"`
	PairCode         string `json:"pair_code,omitempty"`
	Protocol         string `json:"protocol"`
	AuthEnv          string `json:"auth_env,omitempty"`
	ModelUpstream    string `json:"model_upstream,omitempty"`
	ModelListen      string `json:"model_listen"`
	OutputPointer    string `json:"output_pointer,omitempty"`
	PairingID        string `json:"pairing_id,omitempty"`
}

func RunClient(ctx context.Context, directory string, initial ClientConfig) error {
	path := filepath.Join(directory, "connection.json")
	raw, err := os.ReadFile(path)
	c := initial
	if err == nil {
		if initial.PairCode != "" {
			return fmt.Errorf("state exists; restart without pairing arguments or choose another --state-dir")
		}
		if err = json.Unmarshal(raw, &c); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	} else {
		if c.PairCode == "" {
			return fmt.Errorf("first connection requires --pair-code, --server and --local")
		}
		c.Token = Token("eyes_")
		c.ObservationToken = Token("eyes_obs_")
	}
	if _, err = URL(c.Server, true); err != nil {
		return err
	}
	local, err := URL(c.Local, false)
	if err != nil {
		return err
	}
	if c.Protocol != "json" && c.Protocol != "chat_completions" {
		return fmt.Errorf("protocol must be json or chat_completions")
	}
	if c.AuthEnv != "" && os.Getenv(c.AuthEnv) == "" {
		return fmt.Errorf("missing local Agent credential environment variable %s", c.AuthEnv)
	}
	if c.Gateway == "" {
		c.Gateway = strings.TrimSuffix(strings.TrimRight(c.Server, "/"), "/api") + "/tunnel"
	}
	gateway, err := URL(c.Gateway, true)
	if err != nil {
		return err
	}
	if c.ModelUpstream != "" {
		if _, err = URL(c.ModelUpstream, true); err != nil {
			return err
		}
	}
	// Persist redemption identity before sending it; replay only the exact pending redemption.
	if err = Save(path, c); err != nil {
		return err
	}
	if c.PairCode != "" {
		body := map[string]any{"code": c.PairCode, "token": c.Token, "adapter": "tunnel", "url": c.Local, "protocol": c.Protocol, "observation_token": c.ObservationToken, "output_pointer": c.OutputPointer}
		var response struct {
			ID string `json:"id"`
		}
		if err = API(ctx, c.Server, "", "/v1/agent-pairings/redeem", body, &response); err != nil {
			return err
		}
		c.PairingID = response.ID
		c.PairCode = ""
		if err = Save(path, c); err != nil {
			return err
		}
	}
	if c.ModelUpstream != "" {
		proxy, err := ModelProxy(ctx, c, directory)
		if err != nil {
			return err
		}
		defer proxy.Close()
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	localProxy := httputil.ReverseProxy{FlushInterval: -1, Transport: &http.Transport{DisableKeepAlives: true, ResponseHeaderTimeout: 15 * time.Minute}, Rewrite: func(p *httputil.ProxyRequest) {
		p.Out.URL = new(url.URL)
		*p.Out.URL = *local
		p.Out.Host = local.Host
		p.Out.Header = http.Header{"Content-Type": []string{"application/json"}}
		for _, h := range []string{"X-Eyes-Attempt-Id", "X-Eyes-Session-Id", "Traceparent"} {
			if v := p.In.Header.Get(h); v != "" {
				p.Out.Header.Set(h, v)
			}
		}
		if c.AuthEnv != "" {
			p.Out.Header.Set("Authorization", os.Getenv(c.AuthEnv))
		}
	}, ModifyResponse: func(r *http.Response) error { r.Header.Del("Set-Cookie"); return nil }}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /invoke", func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, MaxBody)
		localProxy.ServeHTTP(w, r)
	})
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second, MaxHeaderBytes: 32 << 10}
	defer server.Close()
	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("local forwarder: %v", err)
		}
	}()
	gateway.Path = strings.TrimRight(gateway.Path, "/") + "/connect"
	if gateway.Scheme == "https" {
		gateway.Scheme = "wss"
	} else {
		gateway.Scheme = "ws"
	}
	headers := http.Header{"Authorization": []string{"Bearer " + c.Token}}
	localDialer := func(ctx context.Context, network, address string) (net.Conn, error) {
		return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", listener.Addr().String())
	}
	for ctx.Err() == nil {
		err = remotedialer.ConnectToProxyWithDialer(ctx, gateway.String(), headers, func(network, address string) bool { return network == "tcp" && address == "eyes-agent:80" }, nil, localDialer, func(context.Context, *remotedialer.Session) error {
			log.Printf("connected pairing=%s; target readiness is not yet verified", c.PairingID)
			return nil
		})
		if ctx.Err() != nil {
			break
		}
		log.Printf("connection ended: %v; retrying in 5 seconds", err)
		select {
		case <-ctx.Done():
		case <-time.After(5 * time.Second):
		}
	}
	return nil
}
