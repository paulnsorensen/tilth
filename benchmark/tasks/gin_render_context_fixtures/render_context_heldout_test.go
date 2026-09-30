package render

import (
    "context"
    "errors"
    "html/template"
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
    "time"
)

type countedReader struct { reads int }
func (r *countedReader) Read(p []byte) (int, error) { r.reads++; return 0, io.EOF }

func TestHeldOutRenderCancellation(t *testing.T) {
    reads, executes := &countedReader{}, 0
    tmpl := template.Must(template.New("x").Funcs(template.FuncMap{"mark": func() string { executes++; return "ok" }}).Parse("{{mark}}"))
    req := httptest.NewRequest(http.MethodGet, "/", nil)
    renderers := []struct { name string; value Render }{
        {"JSON", JSON{map[string]any{"x": 1}}},
        {"IndentedJSON", IndentedJSON{map[string]any{"x": 1}}},
        {"SecureJSON", SecureJSON{"prefix", []int{1}}},
        {"JsonpJSON", JsonpJSON{"cb", 1}},
        {"AsciiJSON", AsciiJSON{"é"}},
        {"PureJSON", PureJSON{1}},
        {"XML", XML{1}},
        {"String", String{"hi", nil}},
        {"Redirect", Redirect{http.StatusFound, req, "/next"}},
        {"Data", Data{"custom/type", []byte("hi")}},
        {"HTML", HTML{tmpl, "", nil}},
        {"YAML", YAML{1}},
        {"Reader", Reader{"custom/type", 5, reads, map[string]string{"X-Reader": "yes"}}},
        {"ProtoBuf", ProtoBuf{nil}},
        {"TOML", TOML{map[string]string{"x": "y"}}},
        {"BSON", BSON{map[string]string{"x": "y"}}},
    }
    for _, tc := range renderers {
        for _, cause := range []error{context.Canceled, context.DeadlineExceeded} {
            t.Run(tc.name+cause.Error(), func(t *testing.T) {
                ctx, cancel := context.WithCancel(context.Background())
                if cause == context.DeadlineExceeded {
                    var deadlineCancel context.CancelFunc
                    ctx, deadlineCancel = context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
                    defer deadlineCancel()
                } else { cancel() }
                defer cancel()
                w := httptest.NewRecorder()
                w.Header().Set("X-Preset", "original")
                if err := tc.value.Render(ctx, w); !errors.Is(err, cause) { t.Fatalf("error = %v, want %v", err, cause) }
                if len(w.Header()) != 1 || w.Header().Get("X-Preset") != "original" || w.Body.Len() != 0 {
                    t.Fatalf("cancelled render changed response: headers=%v body=%q", w.Header(), w.Body.String())
                }
            })
        }
    }
    if reads.reads != 0 || executes != 0 { t.Fatalf("cancelled render performed work: reads=%d executes=%d", reads.reads, executes) }
}

func TestHeldOutActiveRenderAndHelpers(t *testing.T) {
    w := httptest.NewRecorder()
    if err := (Reader{ContentType:"custom/type", ContentLength:2, Reader:strings.NewReader("ok")}).Render(context.Background(), w); err != nil { t.Fatal(err) }
    if w.Header().Get("Content-Type") != "custom/type" || w.Header().Get("Content-Length") != "2" || w.Body.String() != "ok" { t.Fatalf("active reader changed: %v %q", w.Header(), w.Body.String()) }
    w = httptest.NewRecorder()
    if err := (HTML{Template:template.Must(template.New("x").Parse("hello"))}).Render(context.Background(), w); err != nil || w.Body.String() != "hello" { t.Fatalf("active HTML: %v %q", err, w.Body.String()) }
    w = httptest.NewRecorder()
    if err := WriteJSON(w, map[string]int{"x":1}); err != nil || w.Body.String() != "{\"x\":1}" { t.Fatalf("WriteJSON: %v %q", err, w.Body.String()) }
    w = httptest.NewRecorder()
    w.Header().Set("Content-Type", "application/custom")
    if err := (JSON{Data: 1}).Render(context.Background(), w); err != nil || w.Header().Get("Content-Type") != "application/custom" || w.Body.String() != "1" { t.Fatalf("custom content type: %v %v %q", err, w.Header(), w.Body.String()) }
    w = httptest.NewRecorder()
    if err := WriteString(w, "%s", []any{"ok"}); err != nil || w.Body.String() != "ok" { t.Fatalf("WriteString: %v %q", err, w.Body.String()) }
}
