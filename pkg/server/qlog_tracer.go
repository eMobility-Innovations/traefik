package server

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/quic-go/quic-go"
	httpqlog "github.com/quic-go/quic-go/http3/qlog"
	"github.com/quic-go/quic-go/qlog"
	"github.com/quic-go/quic-go/qlogwriter"
	"github.com/quic-go/quic-go/qlogwriter/jsontext"
	"github.com/rs/zerolog/log"
)

// These names deliberately avoid the TRAEFIK_ prefix, which Traefik decodes as static configuration.
const (
	qlogDirEnv          = "H3_QLOG_DIR"
	qlogMaxDirBytesEnv  = "H3_QLOG_MAX_DIR_BYTES"
	qlogMinFreeBytesEnv = "H3_QLOG_MIN_FREE_BYTES"
	qlogPruneAgeEnv     = "H3_QLOG_PRUNE_AGE"
)

const (
	// qlogQueueBytes bounds the memory held for a stalled disk; a trace that overflows it is cut.
	qlogQueueBytes = 4 << 20
	// qlogFreeCheckBytes is how many written bytes may pass between free-space checks.
	qlogFreeCheckBytes = 1 << 20
)

// qlogTraceName matches the only files pruning may ever remove.
var qlogTraceName = regexp.MustCompile(`^[0-9a-f]+_server\.sqlog$`)

// qlogConnectionTracer returns the process-wide tracer shared by every HTTP/3 entry point, so
// the size cap covers all of them. It returns nil, leaving QUIC untouched, unless configured.
var qlogConnectionTracer = sync.OnceValue(func() qlogTracerFunc {
	// The worker lives for the whole process, so the stop returned alongside the tracer is dropped here.
	tracer, _ := newQlogTracer(os.Getenv)

	return tracer
})

type qlogTracerFunc = func(ctx context.Context, isClient bool, connID quic.ConnectionID) qlogwriter.Trace

type qlogConfig struct {
	dir          string
	maxDirBytes  int64
	minFreeBytes int64
	pruneAge     time.Duration // Zero means traces are never removed.
}

// qlogIO is the disk as the worker sees it.
type qlogIO struct {
	open      func(path string) (io.WriteCloser, error)
	freeBytes func(dir string) (int64, error)
}

// newQlogTracer returns the tracer and a close that stops its disk worker, or a
// nil tracer when qlog is not fully configured.
func newQlogTracer(getenv func(string) string) (qlogTracerFunc, func()) {
	cfg, err := parseQlogConfig(getenv)
	if err != nil {
		log.Warn().Err(err).Msg("HTTP/3 qlog tracing stays off")
		return nil, func() {}
	}
	if cfg.dir == "" {
		return nil, func() {}
	}

	notify := func(msg string) { log.Warn().Str("dir", cfg.dir).Msg(msg) }
	writer := newQlogWriter(cfg, qlogIO{open: openQlogFile, freeBytes: qlogFreeBytes}, notify)

	return httpqlog.RedactHeaders(writer.trace, redactQlogHeader), writer.close
}

func parseQlogConfig(getenv func(string) string) (qlogConfig, error) {
	cfg := qlogConfig{dir: getenv(qlogDirEnv)}
	if cfg.dir == "" {
		return qlogConfig{}, nil
	}

	var err error
	if cfg.maxDirBytes, err = parseQlogBytes(getenv, qlogMaxDirBytesEnv); err != nil {
		return qlogConfig{}, err
	}
	if cfg.minFreeBytes, err = parseQlogBytes(getenv, qlogMinFreeBytesEnv); err != nil {
		return qlogConfig{}, err
	}

	age := getenv(qlogPruneAgeEnv)
	if age == "" {
		return cfg, nil
	}
	if cfg.pruneAge, err = time.ParseDuration(age); err != nil || cfg.pruneAge <= 0 {
		return qlogConfig{}, fmt.Errorf("%s must be a positive duration, got %q", qlogPruneAgeEnv, age)
	}

	return cfg, nil
}

func parseQlogBytes(getenv func(string) string, name string) (int64, error) {
	value, err := strconv.ParseInt(getenv(name), 10, 64)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive byte count, got %q", name, getenv(name))
	}

	return value, nil
}

func openQlogFile(path string) (io.WriteCloser, error) {
	return os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
}

// redactQlogHeader keeps request cookies, paths and every other header value out of the trace.
func redactQlogHeader(name, value string) string {
	if name == ":method" || name == ":status" {
		return value
	}

	return ""
}

type qlogOpKind int

const (
	qlogOpen qlogOpKind = iota
	qlogWrite
	qlogClose
)

type qlogOp struct {
	kind  qlogOpKind
	trace *qlogFileTrace
	data  []byte
}

// qlogFileTrace is one connection's file. Only cut is touched outside the worker.
type qlogFileTrace struct {
	path string
	file io.WriteCloser
	cut  atomic.Bool // Once set, the trace never writes again, so a file never hides a gap.
}

// qlogWriter keeps all disk work on one goroutine. quic-go calls the tracer from its single accept
// loop and records events from each connection's own loop, so nothing they call may touch the disk.
type qlogWriter struct {
	cfg     qlogConfig
	disk    qlogIO
	notify  func(string)
	stopped atomic.Bool
	wake    chan struct{}
	// stop tells the worker to exit after draining the queue; done closes when it has.
	stop chan struct{}
	done chan struct{}

	mu           sync.Mutex
	pending      []qlogOp
	pendingBytes int
	dead         bool

	// Owned by the worker goroutine.
	used         int64
	sinceFree    int64
	live         map[string]struct{}
	failureNoted bool
	retry        *time.Timer
}

func newQlogWriter(cfg qlogConfig, disk qlogIO, notify func(string)) *qlogWriter {
	w := &qlogWriter{
		cfg:    cfg,
		disk:   disk,
		notify: notify,
		wake:   make(chan struct{}, 1),
		live:   make(map[string]struct{}),
		stop:   make(chan struct{}, 1),
		done:   make(chan struct{}),
	}
	go w.run()

	return w
}

// trace is the quic.Config.Tracer. It must not block and must return a literal nil when not tracing.
func (w *qlogWriter) trace(_ context.Context, isClient bool, connID quic.ConnectionID) qlogwriter.Trace {
	if isClient || w.stopped.Load() {
		return nil
	}

	t := &qlogFileTrace{path: filepath.Join(w.cfg.dir, connID.String()+"_server.sqlog")}
	w.enqueue(qlogOp{kind: qlogOpen, trace: t})

	sink := &qlogSink{writer: w, trace: t}
	buffered := &qlogBufferedSink{Writer: bufio.NewWriter(sink), sink: sink}
	seq := qlogwriter.NewConnectionFileSeq(buffered, false, connID, []string{qlog.EventSchema, httpqlog.EventSchema})
	go seq.Run()

	return qlogSafeTrace{Trace: seq}
}

// enqueue never waits on the disk. Open and close carry no bytes and are always accepted,
// so a file is closed however full the queue was while its connection lived.
func (w *qlogWriter) enqueue(op qlogOp) bool {
	w.mu.Lock()
	if w.dead || w.pendingBytes+len(op.data) > qlogQueueBytes {
		w.mu.Unlock()
		return false
	}
	w.pending = append(w.pending, op)
	w.pendingBytes += len(op.data)
	w.mu.Unlock()

	select {
	case w.wake <- struct{}{}:
	default:
	}

	return true
}

func (w *qlogWriter) run() {
	defer w.recoverWorker()
	defer close(w.done)

	w.start()
	for {
		select {
		case <-w.wake:
			w.drain()
		case <-w.stop:
			// A last drain keeps every accepted op from outliving the worker.
			w.drain()
			return
		case <-w.retryTimer():
			w.prune()
		}
	}
}

// close waits for the worker to finish queued disk work and exit, so a directory can be
// removed without racing a late write. Production never calls it: the worker is a
// process-lifetime resource.
func (w *qlogWriter) close() {
	select {
	case <-w.done:
	case w.stop <- struct{}{}:
	}
	<-w.done
}

func (w *qlogWriter) retryTimer() <-chan time.Time {
	if w.retry == nil {
		return nil
	}

	return w.retry.C
}

func (w *qlogWriter) recoverWorker() {
	r := recover()
	if r == nil {
		return
	}

	w.stopped.Store(true)
	w.mu.Lock()
	w.dead, w.pending = true, nil
	w.mu.Unlock()
	w.notify(fmt.Sprintf("HTTP/3 qlog tracing stopped: worker failed: %v", r))
}

// start measures the directory. If it cannot, the usage is unknown and tracing fails closed.
func (w *qlogWriter) start() {
	if err := os.MkdirAll(w.cfg.dir, 0o700); err != nil {
		w.trip(fmt.Sprintf("cannot create directory: %v", err))
		return
	}

	entries, err := os.ReadDir(w.cfg.dir)
	if err != nil {
		w.trip(fmt.Sprintf("cannot read directory: %v", err))
		return
	}
	for _, entry := range entries {
		if info, err := entry.Info(); err == nil && info.Mode().IsRegular() {
			w.used += info.Size()
		}
	}
}

func (w *qlogWriter) drain() {
	w.mu.Lock()
	ops := w.pending
	w.pending, w.pendingBytes = nil, 0
	w.mu.Unlock()

	for _, op := range ops {
		switch op.kind {
		case qlogOpen:
			w.openTrace(op.trace)
		case qlogWrite:
			w.writeTrace(op.trace, op.data)
		case qlogClose:
			w.closeTrace(op.trace)
		}
	}
}

func (w *qlogWriter) openTrace(t *qlogFileTrace) {
	if reason := w.limitReason(); reason != "" {
		w.trip(reason)
	}
	if w.stopped.Load() {
		t.cut.Store(true)
		return
	}

	file, err := w.disk.open(t.path)
	if err != nil {
		t.cut.Store(true)
		w.noteFailure(err)
		return
	}
	t.file = file
	w.live[t.path] = struct{}{}
}

func (w *qlogWriter) writeTrace(t *qlogFileTrace, data []byte) {
	if t.file == nil || t.cut.Load() {
		return
	}
	if w.stopped.Load() {
		w.cutTrace(t)
		return
	}
	if w.used+int64(len(data)) > w.cfg.maxDirBytes {
		w.cutTrace(t)
		w.trip(w.capReason())
		return
	}

	n, err := t.file.Write(data)
	w.used += int64(n)
	w.sinceFree += int64(n)
	if err != nil {
		w.cutTrace(t)
		w.noteFailure(err)
		return
	}
	if w.sinceFree < qlogFreeCheckBytes {
		return
	}

	w.sinceFree = 0
	if reason := w.floorReason(); reason != "" {
		w.trip(reason)
	}
}

func (w *qlogWriter) cutTrace(t *qlogFileTrace) {
	t.cut.Store(true)
	w.closeTrace(t)
}

func (w *qlogWriter) closeTrace(t *qlogFileTrace) {
	if t.file == nil {
		return
	}
	_ = t.file.Close()
	t.file = nil
	delete(w.live, t.path)
}

func (w *qlogWriter) noteFailure(err error) {
	if w.failureNoted {
		return
	}
	w.failureNoted = true
	w.notify(fmt.Sprintf("HTTP/3 qlog: a connection is not being traced, further failures are not logged: %v", err))
}

func (w *qlogWriter) limitReason() string {
	if w.used >= w.cfg.maxDirBytes {
		return w.capReason()
	}

	return w.floorReason()
}

func (w *qlogWriter) capReason() string {
	return fmt.Sprintf("directory size cap reached: %d of %d bytes", w.used, w.cfg.maxDirBytes)
}

func (w *qlogWriter) floorReason() string {
	free, err := w.disk.freeBytes(w.cfg.dir)
	if err != nil {
		return fmt.Sprintf("free space unreadable: %v", err)
	}
	if free < w.cfg.minFreeBytes {
		return fmt.Sprintf("free space below floor: %d of %d bytes", free, w.cfg.minFreeBytes)
	}

	return ""
}

// trip stops new traces and says so once per stop.
func (w *qlogWriter) trip(reason string) {
	if w.stopped.Swap(true) {
		return
	}
	if w.cfg.pruneAge == 0 {
		w.notify("HTTP/3 qlog tracing stopped until restart: " + reason)
		return
	}

	w.notify("HTTP/3 qlog tracing stopped until pruning frees space: " + reason)
	w.prune()
}

// prune removes expired traces, then resumes or arms one timer for the moment the oldest remaining
// trace expires. There is no polling: without a limit tripped this never runs.
func (w *qlogWriter) prune() {
	entries, err := os.ReadDir(w.cfg.dir)
	if err != nil {
		w.retry = time.NewTimer(w.cfg.pruneAge)
		return
	}

	used, oldest := w.removeExpired(entries, time.Now().Add(-w.cfg.pruneAge))
	w.used = used
	if w.limitReason() != "" {
		w.armRetry(oldest)
		return
	}

	w.retry = nil
	w.stopped.Store(false)
	w.notify("HTTP/3 qlog tracing resumed: pruning freed space")
}

func (w *qlogWriter) armRetry(oldest time.Time) {
	if oldest.IsZero() {
		w.retry = time.NewTimer(w.cfg.pruneAge)
		return
	}
	w.retry = time.NewTimer(time.Until(oldest.Add(w.cfg.pruneAge)))
}

// removeExpired returns the bytes left in the directory and the oldest unexpired trace.
func (w *qlogWriter) removeExpired(entries []os.DirEntry, cutoff time.Time) (int64, time.Time) {
	var used int64
	var oldest time.Time
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			continue
		}

		if !w.ownsTrace(entry.Name()) {
			used += info.Size()
			continue
		}
		if info.ModTime().Before(cutoff) {
			if os.Remove(filepath.Join(w.cfg.dir, entry.Name())) != nil {
				used += info.Size()
			}
			continue
		}

		used += info.Size()
		if oldest.IsZero() || info.ModTime().Before(oldest) {
			oldest = info.ModTime()
		}
	}

	return used, oldest
}

// ownsTrace proves a file is a finished trace from its exact name; anything else is left alone.
func (w *qlogWriter) ownsTrace(name string) bool {
	if !qlogTraceName.MatchString(name) {
		return false
	}
	_, open := w.live[filepath.Join(w.cfg.dir, name)]

	return !open
}

// qlogSink is what the qlog encoder writes to: it never blocks and never fails, so a disk
// problem can neither stall the connection through FileSeq's event channel nor stop its encoder.
type qlogSink struct {
	writer *qlogWriter
	trace  *qlogFileTrace
}

func (s *qlogSink) Write(p []byte) (int, error) {
	if s.trace.cut.Load() {
		return len(p), nil
	}
	if !s.writer.enqueue(qlogOp{kind: qlogWrite, trace: s.trace, data: bytes.Clone(p)}) {
		s.trace.cut.Store(true)
	}

	return len(p), nil
}

type qlogBufferedSink struct {
	*bufio.Writer

	sink *qlogSink
}

func (b *qlogBufferedSink) Close() error {
	_ = b.Flush()
	b.sink.writer.enqueue(qlogOp{kind: qlogClose, trace: b.sink.trace})

	return nil
}

// qlogSafeTrace contains a panic in event encoding, which runs on a goroutine with no other guard.
type qlogSafeTrace struct {
	qlogwriter.Trace
}

func (t qlogSafeTrace) AddProducer() qlogwriter.Recorder {
	recorder := t.Trace.AddProducer()
	if recorder == nil {
		return nil
	}

	return qlogSafeRecorder{Recorder: recorder}
}

type qlogSafeRecorder struct {
	qlogwriter.Recorder
}

func (r qlogSafeRecorder) RecordEvent(event qlogwriter.Event) {
	r.Recorder.RecordEvent(qlogSafeEvent{Event: event})
}

type qlogSafeEvent struct {
	qlogwriter.Event
}

func (e qlogSafeEvent) Encode(encoder *jsontext.Encoder, eventTime time.Time) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = errors.New("qlog event encoding panicked")
		}
	}()

	return e.Event.Encode(encoder, eventTime)
}
