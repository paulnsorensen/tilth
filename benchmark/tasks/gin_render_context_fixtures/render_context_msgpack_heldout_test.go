//go:build !nomsgpack

package render

import (
    "context"
    "errors"
    "net/http/httptest"
    "testing"
)

func TestHeldOutMsgPackCancellation(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    cancel()
    w := httptest.NewRecorder()
    err := (MsgPack{Data: map[string]int{"x": 1}}).Render(ctx, w)
    if !errors.Is(err, context.Canceled) || len(w.Header()) != 0 || w.Body.Len() != 0 {
        t.Fatalf("cancelled MsgPack: err=%v headers=%v body=%q", err, w.Header(), w.Body.String())
    }
}
