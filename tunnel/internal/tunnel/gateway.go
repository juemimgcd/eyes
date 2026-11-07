package tunnel

import (
	"bufio"
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"strings"
	"sync"
	"time"

	"github.com/rancher/remotedialer"
)

func Gateway(api, downloads string) http.Handler {
	type key struct{}
	dialer := remotedialer.New(func(r *http.Request) (string, bool, error) {
		id, ok := r.Context().Value(key{}).(string)
		return id, ok, nil
	}, remotedialer.DefaultErrorWriter)
	// Clients may answer server requests, but must never dial the gateway's private network.
	dialer.ClientConnectAuthorizer = func(string, string) bool { return false }
	var mu sync.Mutex
	active := map[string]bool{}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /connect", func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		var identity struct {
			ID string `json:"id"`
		}
		if err := API(r.Context(), api, token, "/v1/tunnel/session", nil, &identity); err != nil {
			http.Error(w, "tunnel authentication failed", 401)
			return
		}
		mu.Lock()
		duplicate := active[identity.ID]
		if !duplicate {
			active[identity.ID] = true
		}
		mu.Unlock()
		if duplicate {
			http.Error(w, "connection already active", 409)
			return
		}
		defer func() { mu.Lock(); delete(active, identity.ID); mu.Unlock() }()
		ctx, cancel := context.WithCancel(context.WithValue(r.Context(), key{}, identity.ID))
		defer cancel()
		go func() {
			ticker := time.NewTicker(10 * time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-ctx.Done():
					return
				case <-ticker.C:
					if err := API(ctx, api, token, "/v1/tunnel/session", nil, nil); err != nil {
						log.Printf("connection authorization lost id=%s: %v", identity.ID, err)
						cancel()
						return
					}
				}
			}
		}()
		dialer.ServeHTTP(&connectionWriter{ResponseWriter: w, ctx: ctx}, r.WithContext(ctx))
	})
	mux.HandleFunc("POST /invoke/{id}", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		if len(id) != 36 {
			http.Error(w, "invalid connection id", 400)
			return
		}
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if err := API(r.Context(), api, token, "/v1/tunnel/authorize/"+id, nil, nil); err != nil {
			http.Error(w, "target authorization failed", 403)
			return
		}
		if !dialer.HasSession(id) {
			http.Error(w, "Agent offline; execution unconfirmed", 503)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, MaxBody)
		transport := &http.Transport{DialContext: dialer.Dialer(id), DisableKeepAlives: true, ResponseHeaderTimeout: 15 * time.Minute}
		defer transport.CloseIdleConnections()
		proxy := httputil.ReverseProxy{Transport: transport, FlushInterval: -1, Rewrite: func(p *httputil.ProxyRequest) {
			p.Out.URL.Scheme = "http"
			p.Out.URL.Host = "eyes-agent:80"
			p.Out.URL.Path = "/invoke"
			p.Out.URL.RawPath = ""
			p.Out.URL.RawQuery = ""
			p.Out.Host = "eyes-agent"
			// An internal Runner token must never reach the Agent. Avoid transport replay hints.
			p.Out.Header = http.Header{"Content-Type": []string{"application/json"}}
			for _, h := range []string{"X-Eyes-Attempt-Id", "X-Eyes-Session-Id", "Traceparent"} {
				if v := p.In.Header.Get(h); v != "" {
					p.Out.Header.Set(h, v)
				}
			}
		}, ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			log.Printf("tunnel request id=%s: %v", id, err)
			http.Error(w, "tunnel interrupted; execution unconfirmed", 502)
		}}
		proxy.ServeHTTP(w, r)
	})
	if downloads != "" {
		files := http.FileServer(http.Dir(downloads))
		mux.Handle("GET /downloads/", http.StripPrefix("/downloads/", files))
	}
	mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprintln(w, "ok") })
	return mux
}

// remotedialer Serve reads the socket directly; cancelling its context alone does
// not interrupt NextReader. Close the hijacked socket when authorization expires.
type connectionWriter struct {
	http.ResponseWriter
	ctx context.Context
}

func (w *connectionWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	conn, buffer, err := http.NewResponseController(w.ResponseWriter).Hijack()
	if err != nil {
		return nil, nil, err
	}
	go func() { <-w.ctx.Done(); conn.Close() }()
	return conn, buffer, nil
}
