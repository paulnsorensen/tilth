package gin

import (
    "context"
    "errors"
    "net/http"
    "net/http/httptest"
    "testing"
    "time"
    "github.com/gin-gonic/gin/render"
)

type heldOutKey struct{}
type heldOutRender struct { got context.Context; calls int; err error; contentTypes int }
func (r *heldOutRender) Render(ctx context.Context, w http.ResponseWriter) error { r.got = ctx; r.calls++; return r.err }
func (r *heldOutRender) WriteContentType(w http.ResponseWriter) { r.contentTypes++; w.Header().Set("Content-Type", "custom/type") }

func TestHeldOutContextRenderDispatch(t *testing.T) {
    parent := context.WithValue(context.Background(), heldOutKey{}, "value")
    deadline := time.Now().Add(time.Minute)
    parent, cancel := context.WithDeadline(parent, deadline)
    defer cancel()
    request := httptest.NewRequest(http.MethodGet, "/", nil).WithContext(parent)
    w := httptest.NewRecorder()
    c, _ := CreateTestContext(w)
    c.Request = request
    renderer := &heldOutRender{}
    c.Render(http.StatusCreated, renderer)
    if renderer.calls != 1 || renderer.got != parent || renderer.got.Value(heldOutKey{}) != "value" { t.Fatalf("context identity/value lost: %#v", renderer) }
    if got, ok := renderer.got.Deadline(); !ok || !got.Equal(deadline) { t.Fatalf("deadline lost: %v %v", got, ok) }
    if c.Writer.Status() != http.StatusCreated { t.Fatalf("status = %d", c.Writer.Status()) }

    w = httptest.NewRecorder()
    c, _ = CreateTestContext(w)
    renderer = &heldOutRender{}
    c.Request = nil
    c.Render(http.StatusOK, renderer)
    if renderer.calls != 1 || renderer.got != context.Background() { t.Fatalf("nil request context: %#v", renderer) }
}

func TestHeldOutContextRenderErrorAndNoBody(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    cancel()
    w := httptest.NewRecorder()
    c, _ := CreateTestContext(w)
    c.Request = httptest.NewRequest(http.MethodGet, "/", nil).WithContext(ctx)
    c.Render(http.StatusOK, render.JSON{Data: map[string]int{"x":1}})
    if len(c.Errors) != 1 || !errors.Is(c.Errors[0].Err, context.Canceled) || !c.IsAborted() || w.Body.Len() != 0 || w.Header().Get("Content-Type") != "" {
        t.Fatalf("cancelled dispatch: errors=%v aborted=%v headers=%v body=%q", c.Errors, c.IsAborted(), w.Header(), w.Body.String())
    }
    w = httptest.NewRecorder()
    c, _ = CreateTestContext(w)
    renderer := &heldOutRender{}
    c.Render(http.StatusNoContent, renderer)
    if renderer.calls != 0 || renderer.contentTypes != 1 || w.Code != http.StatusNoContent || w.Header().Get("Content-Type") != "custom/type" {
        t.Fatalf("bodyless render: %#v status=%d headers=%v", renderer, w.Code, w.Header())
    }
}

func TestHeldOutSSEventPath(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    cancel()
    w := httptest.NewRecorder()
    c, _ := CreateTestContext(w)
    c.Request = httptest.NewRequest(http.MethodGet, "/", nil).WithContext(ctx)
    c.SSEvent("ping", "hello")
    if len(c.Errors) != 1 || !errors.Is(c.Errors[0].Err, context.Canceled) || !c.IsAborted() || len(w.Header()) != 0 || w.Body.Len() != 0 {
        t.Fatalf("cancelled SSE: errors=%v aborted=%v headers=%v body=%q", c.Errors, c.IsAborted(), w.Header(), w.Body.String())
    }
    w = httptest.NewRecorder()
    c, _ = CreateTestContext(w)
    c.SSEvent("ping", "hello")
    if w.Body.String() != "event:ping\ndata:hello\n\n" { t.Fatalf("active SSE: %q", w.Body.String()) }
}
