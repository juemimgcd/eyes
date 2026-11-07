package tunnel

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Observation uses the existing API. Each model call is independent unless the
// Agent supplies an explicit trace ID; elapsed time is never an identity.
func ModelProxy(ctx context.Context, c ClientConfig, directory string) (*http.Server, error) {
	host, _, err := net.SplitHostPort(c.ModelListen)
	if err != nil {
		return nil, err
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return nil, fmt.Errorf("model proxy must bind a loopback IP")
	}
	listener, err := net.Listen("tcp", c.ModelListen)
	if err != nil {
		return nil, err
	}
	spool := filepath.Join(directory, "observations")
	if err = os.MkdirAll(spool, 0700); err != nil {
		listener.Close()
		return nil, err
	}
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				entries, err := os.ReadDir(spool)
				if err != nil {
					log.Printf("observation queue: %v", err)
					continue
				}
				for _, entry := range entries {
					if !strings.HasSuffix(entry.Name(), ".json") {
						continue
					}
					path := filepath.Join(spool, entry.Name())
					raw, err := os.ReadFile(path)
					if err != nil {
						log.Printf("observation read: %v", err)
						continue
					}
					var batch map[string]any
					if err = json.Unmarshal(raw, &batch); err != nil {
						log.Printf("invalid observation file %s: %v", entry.Name(), err)
						continue
					}
					if err = API(ctx, c.Server, c.ObservationToken, "/v1/observation/events", batch, nil); err != nil {
						var apiErr *APIError
						if errors.As(err, &apiErr) && (apiErr.Status == 400 || apiErr.Status == 409 || apiErr.Status == 413 || apiErr.Status == 422) {
							if moveErr := os.Rename(path, path+".rejected"); moveErr != nil {
								log.Printf("observation reject move: %v", moveErr)
							}
							log.Printf("observation rejected file=%s: %v", entry.Name(), err)
							continue
						}
						log.Printf("observation upload retained: %v", err)
						break
					}
					// Start and terminal batches use different files, so an ACK cannot delete newer data.
					if err = os.Remove(path); err != nil {
						log.Printf("observation ACK cleanup: %v", err)
					}
				}

			}
		}
	}()
	transport := &http.Transport{Proxy: http.ProxyFromEnvironment, DisableKeepAlives: true, ResponseHeaderTimeout: 15 * time.Minute, DisableCompression: true}
	client := &http.Client{Transport: transport, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/chat/completions", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBody))
		if err != nil {
			http.Error(w, "model request exceeds body limit", 413)
			return
		}
		var input map[string]any
		if err = json.Unmarshal(body, &input); err != nil {
			http.Error(w, "request must be JSON", 400)
			return
		}
		if _, ok := input["messages"].([]any); !ok {
			http.Error(w, "messages array required", 400)
			return
		}
		id := ID()
		traceID := strings.ReplaceAll(id, "-", "")
		spanID := traceID[:16]
		session := r.Header.Get("X-Eyes-Trace-ID")
		if len(session) > 128 {
			http.Error(w, "trace ID exceeds 128 characters", 400)
			return
		}
		if session == "" {
			session = id
		}
		model, _ := input["model"].(string)
		if model == "" {
			model = "unknown"
		}
		if len(model) > 200 {
			http.Error(w, "model exceeds 200 characters", 400)
			return
		}
		auth := r.Header.Get("Authorization")
		secret := strings.TrimPrefix(auth, "Bearer ")
		event := func(seq int, kind string, data map[string]any, truncated bool) map[string]any {
			return map[string]any{"event_id": ID(), "sequence": seq, "occurred_at": time.Now().UTC().Format(time.RFC3339Nano), "type": kind, "data": data, "truncated": truncated, "trace_id": traceID, "span_id": spanID}
		}
		batch := func(events ...map[string]any) map[string]any {
			return map[string]any{"run_id": id, "session_id": session, "agent": "Go model proxy (model_protocol_only)", "model": model, "capture_body": true, "events": events}
		}
		queue := func(suffix string, value any) {
			entries, readErr := os.ReadDir(spool)
			// Bound offline capture disk use. Loss is logged; never block the Agent for telemetry.
			if readErr != nil {
				log.Printf("observation queue unavailable: %v", readErr)
				return
			}
			if len(entries) >= 1000 {
				log.Printf("observation dropped run=%s: queue limit 1000 files", id)
				return
			}
			if err := Save(filepath.Join(spool, id+suffix+".json"), value); err != nil {
				log.Printf("observation dropped run=%s: %v", id, err)
			}
		}
		requestData, truncated := preview(input, secret)
		queue("-start", batch(event(1, "run_start", map[string]any{"coverage": "model_protocol_only"}, false), event(2, "model_start", map[string]any{"request": requestData, "coverage": "model_protocol_only", "tool_execution_observed": false}, truncated)))
		// Each live request owns its heartbeat; neither spool backlog nor another
		// slow heartbeat may make this request appear disconnected.
		heartbeatCtx, stopHeartbeat := context.WithCancel(ctx)
		defer stopHeartbeat()
		go func() {
			ticker := time.NewTicker(10 * time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-heartbeatCtx.Done():
					return
				case <-ticker.C:
					if err := API(heartbeatCtx, c.Server, c.ObservationToken, "/v1/observation/heartbeat?run_id="+id, nil, nil); err != nil && heartbeatCtx.Err() == nil {
						log.Printf("observation heartbeat run=%s: %v", id, err)
					}
				}
			}
		}()
		start := time.Now()
		status := "completed"
		capture := &captureBuffer{limit: 1 << 20}
		httpStatus := 0
		failure := ""
		streaming := false
		defer func() {
			response, issues := modelResponse(capture.data, streaming)
			if capture.truncated {
				issues = append(issues, "capture_byte_limit")
			}
			transportIssue := failure
			if parsed, ok := response.(map[string]any); ok && parsed["stream_done"] == true && failure == "downstream_disconnected" {
				// SDKs may close immediately on [DONE]; the model response is still complete.
				status = "completed"
				failure = ""
			}
			if failure != "" {
				issues = append(issues, failure)
			}
			// An upstream error is an execution failure, independent of capture quality.
			if parsed, ok := response.(map[string]any); ok && parsed["error"] != nil {
				status = "failed"
			}
			if len(issues) > 0 && status == "completed" {
				status = "limited"
			}
			result, cut := preview(response, secret)
			modelStatus := status
			if status == "failed" {
				modelStatus = "ERROR"
			}
			endData := map[string]any{
				"response": result, "coverage": "model_protocol_only", "status": modelStatus,
				"http_status": httpStatus, "duration_ms": time.Since(start).Milliseconds(),
				"capture_issues": issues, "transport_issue": transportIssue,
				"tool_execution_observed": false,
			}
			if parsed, ok := response.(map[string]any); ok {
				if usage, ok := parsed["usage"].(map[string]any); ok {
					counts := map[string]any{}
					for from, to := range map[string]string{"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "total_tokens": "total_tokens"} {
						if count, ok := usage[from].(float64); ok && count >= 0 && count == float64(int64(count)) {
							counts[to] = int64(count)
						}
					}
					endData["usage"] = counts
				}
			}
			queue("-end", batch(
				event(3, "model_end", endData, cut || len(issues) > 0),
				event(4, "run_end", map[string]any{"status": status, "dropped_events": 0, "coverage": "model_protocol_only"}, false),
			))
		}()
		req, err := http.NewRequestWithContext(r.Context(), "POST", strings.TrimRight(c.ModelUpstream, "/")+"/chat/completions", bytes.NewReader(body))
		if err != nil {
			failure = "invalid_upstream_request"
			status = "failed"
			http.Error(w, failure, 502)
			return
		}
		for _, h := range []string{"Authorization", "Content-Type", "Accept", "OpenAI-Organization", "OpenAI-Project"} {
			req.Header.Set(h, r.Header.Get(h))
		}
		req.Header.Set("Accept-Encoding", "identity")
		resp, err := client.Do(req)
		if err != nil {
			failure = "upstream_transport_error"
			status = "failed"
			log.Printf("model request run=%s: %v", id, err)
			http.Error(w, failure, 502)
			return
		}
		defer resp.Body.Close()
		httpStatus = resp.StatusCode
		if httpStatus >= 300 {
			status = "failed"
		}
		streaming = strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream")
		for _, h := range []string{"Content-Type", "Cache-Control", "X-Request-Id"} {
			if value := resp.Header.Get(h); value != "" {
				w.Header().Set(h, value)
			}
		}
		w.Header().Set("X-Eyes-Request-Id", id)
		w.WriteHeader(resp.StatusCode)
		buf := make([]byte, 32<<10)
		for {
			n, readErr := resp.Body.Read(buf)
			if n > 0 {
				capture.Write(buf[:n])
				if _, err = w.Write(buf[:n]); err != nil {
					failure = "downstream_disconnected"
					status = "cancelled"
					return
				}
				if f, ok := w.(http.Flusher); ok {
					f.Flush()
				}
			}
			if readErr != nil {
				if !errors.Is(readErr, io.EOF) {
					failure = "upstream_stream_interrupted"
					status = "failed"
					log.Printf("model response interrupted run=%s: %v", id, readErr)
					// Headers are already sent. Abort the response instead of sending
					// a clean EOF that streaming SDKs could mistake for completion.
					panic(http.ErrAbortHandler)
				}
				return
			}
		}
	})
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second, MaxHeaderBytes: 32 << 10}
	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("model proxy: %v", err)
		}
	}()
	log.Printf("model proxy listening at http://%s/v1; captures model communication only", c.ModelListen)
	return server, nil
}

type captureBuffer struct {
	data      []byte
	limit     int
	truncated bool
}

func (c *captureBuffer) Write(p []byte) (int, error) {
	n := len(p)
	remaining := c.limit - len(c.data)
	if len(p) > remaining {
		c.truncated = true
		p = p[:remaining]
	}
	c.data = append(c.data, p...)
	return n, nil
}

func preview(value any, secret string) (any, bool) {
	var redact func(any) any
	redact = func(v any) any {
		switch x := v.(type) {
		case map[string]any:
			out := map[string]any{}
			for k, v := range x {
				lower := strings.ToLower(strings.ReplaceAll(k, "-", "_"))
				if lower == "token" || lower == "access_token" || lower == "refresh_token" || strings.Contains(lower, "secret") || strings.Contains(lower, "password") || strings.Contains(lower, "api_key") || lower == "authorization" || lower == "cookie" || lower == "apikey" || lower == "proxy_authorization" {
					out[k] = "[REDACTED]"
				} else {
					if lower == "arguments" {
						if text, ok := v.(string); ok {
							var parsed any
							if json.Unmarshal([]byte(text), &parsed) == nil {
								v = parsed
							}
						}
					}
					out[k] = redact(v)
				}
			}
			return out
		case []any:
			out := make([]any, len(x))
			for i, v := range x {
				out[i] = redact(v)
			}
			return out
		case string:
			if secret != "" {
				x = strings.ReplaceAll(x, secret, "[REDACTED]")
			}
			return x
		default:
			return v
		}
	}
	value = redact(value)
	raw, err := json.Marshal(value)
	if err != nil {
		return map[string]any{"capture_omitted": "encoding_failed"}, true
	}
	if len(raw) > 16000 {
		return map[string]any{"preview": string(bytes.ToValidUTF8(raw[:16000], []byte("�"))), "capture_omitted": "event_byte_limit"}, true
	}
	return value, false
}

// Chat Completions deltas are assembled before redaction so split tool arguments
// and split credentials cannot leak via individual raw chunks.
func modelResponse(raw []byte, stream bool) (any, []string) {
	if !stream {
		var value any
		if err := json.Unmarshal(raw, &value); err != nil {
			return map[string]any{"capture_omitted": "invalid_json"}, []string{"invalid_json"}
		}
		return value, nil
	}
	frames := strings.Split(strings.ReplaceAll(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\r", "\n"), "\n\n")
	choices := map[int]map[string]any{}
	calls := map[int]map[int]map[string]any{}
	response := map[string]any{"stream_done": false}
	issues := []string{}
	for _, frame := range frames[:len(frames)-1] {
		lines := []string{}
		for _, line := range strings.Split(frame, "\n") {
			if strings.HasPrefix(line, "data:") {
				lines = append(lines, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
			}
		}
		data := strings.Join(lines, "\n")
		if data == "" {
			continue
		}
		if strings.TrimSpace(data) == "[DONE]" {
			response["stream_done"] = true
			break
		}
		var chunk struct {
			ID      string         `json:"id"`
			Model   string         `json:"model"`
			Usage   map[string]any `json:"usage"`
			Error   any            `json:"error"`
			Choices []struct {
				Index        int                        `json:"index"`
				FinishReason *string                    `json:"finish_reason"`
				Delta        map[string]json.RawMessage `json:"delta"`
			} `json:"choices"`
		}
		if err := json.Unmarshal([]byte(data), &chunk); err != nil || strings.TrimSpace(data) == "null" {
			issues = append(issues, "invalid_sse_chunk")
			continue
		}
		if chunk.ID != "" {
			response["id"] = chunk.ID
		}
		if chunk.Model != "" {
			response["model"] = chunk.Model
		}
		if chunk.Usage != nil {
			response["usage"] = chunk.Usage
		}
		if chunk.Error != nil {
			response["error"] = chunk.Error
			issues = append(issues, "upstream_error")
		}
		for _, choice := range chunk.Choices {
			message, ok := choices[choice.Index]
			if !ok {
				message = map[string]any{}
				choices[choice.Index] = message
			}
			if choice.FinishReason != nil {
				message["finish_reason"] = *choice.FinishReason
			}
			for key, rawValue := range choice.Delta {
				switch key {
				case "role", "content", "refusal":
					var text string
					if err := json.Unmarshal(rawValue, &text); err != nil {
						issues = append(issues, "unsupported_delta_value")
						continue
					}
					previous, _ := message[key].(string)
					if key == "role" {
						message[key] = text
					} else {
						message[key] = previous + text
					}
				case "tool_calls":
					var tools []struct {
						Index    int    `json:"index"`
						ID       string `json:"id"`
						Type     string `json:"type"`
						Function struct {
							Name      string `json:"name"`
							Arguments string `json:"arguments"`
						} `json:"function"`
					}
					if err := json.Unmarshal(rawValue, &tools); err != nil {
						issues = append(issues, "invalid_tool_delta")
						continue
					}
					if calls[choice.Index] == nil {
						calls[choice.Index] = map[int]map[string]any{}
					}
					for _, tool := range tools {
						target, ok := calls[choice.Index][tool.Index]
						if !ok {
							target = map[string]any{"function": map[string]any{"name": "", "arguments": ""}}
							calls[choice.Index][tool.Index] = target
						}
						if tool.ID != "" {
							target["id"] = tool.ID
						}
						if tool.Type != "" {
							target["type"] = tool.Type
						}
						function := target["function"].(map[string]any)
						function["name"] = function["name"].(string) + tool.Function.Name
						function["arguments"] = function["arguments"].(string) + tool.Function.Arguments
					}
				default:
					issues = append(issues, "unsupported_delta_field:"+key)
				}
			}
		}
	}
	indexes := make([]int, 0, len(choices))
	for index := range choices {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)
	output := []any{}
	for _, index := range indexes {
		message := choices[index]
		toolIndexes := make([]int, 0, len(calls[index]))
		for ti := range calls[index] {
			toolIndexes = append(toolIndexes, ti)
		}
		sort.Ints(toolIndexes)
		if len(toolIndexes) > 0 {
			tools := []any{}
			for _, ti := range toolIndexes {
				tools = append(tools, calls[index][ti])
			}
			message["tool_calls"] = tools
		}
		choice := map[string]any{"index": index, "message": message}
		if reason, ok := message["finish_reason"]; ok {
			choice["finish_reason"] = reason
			delete(message, "finish_reason")
		}
		output = append(output, choice)
	}
	response["choices"] = output
	if response["stream_done"] != true {
		issues = append(issues, "missing_stream_done")
	}
	sort.Strings(issues)
	unique := []string{}
	for _, issue := range issues {
		if len(unique) == 0 || unique[len(unique)-1] != issue {
			unique = append(unique, issue)
		}
	}
	return response, unique
}
