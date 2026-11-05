package server

import (
	"context"
	"crypto/tls"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"
	"github.com/quic-go/quic-go/qlogwriter/jsontext"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

const qlogTestWait = 5 * time.Second

type qlogTestEvent struct {
	payload string
	panics  bool
}

func (e qlogTestEvent) Name() string { return "test:event" }

func (e qlogTestEvent) Encode(encoder *jsontext.Encoder, _ time.Time) error {
	if e.panics {
		panic("encode failed")
	}

	return encoder.WriteToken(jsontext.String(e.payload))
}

type qlogNotes struct {
	mu    sync.Mutex
	notes []string
}

func (n *qlogNotes) add(msg string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.notes = append(n.notes, msg)
}

func (n *qlogNotes) count(substr string) int {
	n.mu.Lock()
	defer n.mu.Unlock()

	var count int
	for _, note := range n.notes {
		if strings.Contains(note, substr) {
			count++
		}
	}

	return count
}

func plentyFree(string) (int64, error) { return 1 << 50, nil }

func newQlogTestWriter(t *testing.T, cfg qlogConfig, disk qlogIO) (*qlogWriter, *qlogNotes) {
	t.Helper()

	if cfg.dir == "" {
		cfg.dir = t.TempDir()
	}
	if cfg.maxDirBytes == 0 {
		cfg.maxDirBytes = 1 << 30
	}
	if cfg.minFreeBytes == 0 {
		cfg.minFreeBytes = 1
	}
	if disk.open == nil {
		disk.open = openQlogFile
	}
	if disk.freeBytes == nil {
		disk.freeBytes = plentyFree
	}

	notes := &qlogNotes{}

	return newQlogWriter(cfg, disk, notes.add), notes
}

func qlogConnID(n int) quic.ConnectionID {
	return quic.ConnectionIDFromBytes([]byte{0xab, byte(n >> 8), byte(n)})
}

// recordQlogTrace runs one traced connection to its close and reports whether it was traced.
func recordQlogTrace(w *qlogWriter, n, events int, payload string) bool {
	trace := w.trace(context.Background(), false, qlogConnID(n))
	if trace == nil {
		return false
	}

	recorder := trace.AddProducer()
	for range events {
		recorder.RecordEvent(qlogTestEvent{payload: payload})
	}
	_ = recorder.Close()

	return true
}

func qlogTracePath(w *qlogWriter, n int) string {
	return filepath.Join(w.cfg.dir, qlogConnID(n).String()+"_server.sqlog")
}

func requireQlogFile(t *testing.T, path string) {
	t.Helper()
	require.Eventually(t, func() bool {
		_, err := os.Stat(path)
		return err == nil
	}, qlogTestWait, time.Millisecond)
}

func qlogEnv(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func TestQlogOffUnlessFullyConfigured(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "qlog")
	valid := map[string]string{qlogDirEnv: dir, qlogMaxDirBytesEnv: "1000", qlogMinFreeBytesEnv: "1000"}

	without := func(name string) map[string]string {
		values := map[string]string{}
		for key, value := range valid {
			if key != name {
				values[key] = value
			}
		}

		return values
	}
	with := func(name, value string) map[string]string {
		values := without(name)
		values[name] = value

		return values
	}

	testCases := map[string]map[string]string{
		"no directory":    without(qlogDirEnv),
		"no cap":          without(qlogMaxDirBytesEnv),
		"no floor":        without(qlogMinFreeBytesEnv),
		"zero cap":        with(qlogMaxDirBytesEnv, "0"),
		"negative cap":    with(qlogMaxDirBytesEnv, "-1"),
		"bad floor":       with(qlogMinFreeBytesEnv, "abc"),
		"zero floor":      with(qlogMinFreeBytesEnv, "0"),
		"bad age":         with(qlogPruneAgeEnv, "soon"),
		"zero age":        with(qlogPruneAgeEnv, "0s"),
		"negative age":    with(qlogPruneAgeEnv, "-1h"),
		"unitless number": with(qlogPruneAgeEnv, "3600"),
	}
	for name, env := range testCases {
		t.Run(name, func(t *testing.T) {
			assert.Nil(t, newQlogTracer(qlogEnv(env)))
			assert.NoDirExists(t, dir)
		})
	}

	assert.NotNil(t, newQlogTracer(qlogEnv(valid)))
}

func TestQlogSettingNamesAreNotTraefikStaticConfig(t *testing.T) {
	for _, name := range []string{qlogDirEnv, qlogMaxDirBytesEnv, qlogMinFreeBytesEnv, qlogPruneAgeEnv} {
		assert.False(t, strings.HasPrefix(strings.ToUpper(name), "TRAEFIK"), name)
	}
}

func TestQlogClientConnectionsAreNotTraced(t *testing.T) {
	w, _ := newQlogTestWriter(t, qlogConfig{}, qlogIO{})

	assert.Nil(t, w.trace(context.Background(), true, qlogConnID(1)))
}

func TestQlogDirectoryFailureStopsTracingWithoutCrashing(t *testing.T) {
	file := filepath.Join(t.TempDir(), "file")
	require.NoError(t, os.WriteFile(file, nil, 0o600))

	w, notes := newQlogTestWriter(t, qlogConfig{dir: filepath.Join(file, "qlog")}, qlogIO{})

	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)
	assert.False(t, recordQlogTrace(w, 1, 1, "x"))
	assert.Equal(t, 1, notes.count("cannot create directory"))
}

func TestQlogOpenFailureLosesOnlyThatConnection(t *testing.T) {
	var calls atomic.Int32
	open := func(path string) (io.WriteCloser, error) {
		if calls.Add(1) <= 2 {
			return nil, errors.New("no descriptors")
		}

		return openQlogFile(path)
	}
	w, notes := newQlogTestWriter(t, qlogConfig{}, qlogIO{open: open})

	assert.True(t, recordQlogTrace(w, 1, 1, "x"))
	assert.True(t, recordQlogTrace(w, 2, 1, "x"))
	assert.True(t, recordQlogTrace(w, 3, 1, "x"))

	requireQlogFile(t, qlogTracePath(w, 3))
	assert.NoFileExists(t, qlogTracePath(w, 1))
	assert.False(t, w.stopped.Load())
	assert.Equal(t, 1, notes.count("not being traced"))
}

func TestQlogWriteFailureLosesOnlyThatConnection(t *testing.T) {
	var closed atomic.Int32
	open := func(string) (io.WriteCloser, error) {
		return &qlogTestFile{fail: true, closed: &closed}, nil
	}
	w, notes := newQlogTestWriter(t, qlogConfig{}, qlogIO{open: open})

	assert.True(t, recordQlogTrace(w, 1, 1, "x"))
	require.Eventually(t, func() bool {
		return closed.Load() == 1 && notes.count("not being traced") == 1
	}, qlogTestWait, time.Millisecond)
	assert.False(t, w.stopped.Load())
}

type qlogTestFile struct {
	fail   bool
	closed *atomic.Int32
}

func (f *qlogTestFile) Write(p []byte) (int, error) {
	if f.fail {
		return 0, errors.New("disk failed")
	}

	return len(p), nil
}

func (f *qlogTestFile) Close() error {
	f.closed.Add(1)
	return nil
}

func TestQlogStalledDiskBlocksNothingAndLeaksNoFile(t *testing.T) {
	release := make(chan struct{})
	var opened, closed atomic.Int32
	open := func(string) (io.WriteCloser, error) {
		<-release
		opened.Add(1)

		return &qlogTestFile{closed: &closed}, nil
	}
	w, _ := newQlogTestWriter(t, qlogConfig{}, qlogIO{open: open})

	done := make(chan struct{})
	go func() {
		defer close(done)
		payload := strings.Repeat("x", 1024)
		for n := range 2 {
			recordQlogTrace(w, n, 10000, payload)
		}
	}()

	select {
	case <-done:
	case <-time.After(qlogTestWait):
		close(release)
		t.Fatal("a stalled disk blocked the tracer or a recorder")
	}

	w.mu.Lock()
	queued := w.pendingBytes
	w.mu.Unlock()
	assert.LessOrEqual(t, queued, qlogQueueBytes)

	close(release)
	require.Eventually(t, func() bool {
		return opened.Load() == 2 && closed.Load() == 2
	}, qlogTestWait, time.Millisecond)
}

func TestQlogCapStopsNewTracesAndSaysSoOnce(t *testing.T) {
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "existing"), make([]byte, 100), 0o600))

	w, notes := newQlogTestWriter(t, qlogConfig{dir: dir, maxDirBytes: 100}, qlogIO{})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	for n := range 20 {
		assert.False(t, recordQlogTrace(w, n+2, 1, "x"))
	}
	assert.NoFileExists(t, qlogTracePath(w, 1))
	assert.Equal(t, 1, notes.count("size cap reached"))
}

func TestQlogCapHoldsAgainstOneLiveConnection(t *testing.T) {
	const maxDirBytes = 64 << 10

	w, notes := newQlogTestWriter(t, qlogConfig{maxDirBytes: maxDirBytes}, qlogIO{})

	require.True(t, recordQlogTrace(w, 1, 2000, strings.Repeat("x", 200)))
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	info, err := os.Stat(qlogTracePath(w, 1))
	require.NoError(t, err)
	assert.LessOrEqual(t, info.Size(), int64(maxDirBytes))
	assert.Positive(t, info.Size())
	assert.Equal(t, 1, notes.count("size cap reached"))
}

func TestQlogFloorStopsNewTraces(t *testing.T) {
	low := func(string) (int64, error) { return 10, nil }
	w, notes := newQlogTestWriter(t, qlogConfig{minFreeBytes: 11}, qlogIO{freeBytes: low})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	assert.False(t, recordQlogTrace(w, 2, 1, "x"))
	assert.NoFileExists(t, qlogTracePath(w, 1))
	assert.Equal(t, 1, notes.count("below floor"))
}

func TestQlogOneStopIsReportedOnceAcrossQueuedConnections(t *testing.T) {
	release := make(chan struct{})
	low := func(string) (int64, error) {
		<-release
		return 10, nil
	}
	w, notes := newQlogTestWriter(t, qlogConfig{minFreeBytes: 11}, qlogIO{freeBytes: low})

	var traced int
	for n := range 20 {
		if recordQlogTrace(w, n, 1, "x") {
			traced++
		}
	}
	close(release)
	require.Equal(t, 20, traced)
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	require.Never(t, func() bool { return notes.count("below floor") != 1 }, 50*time.Millisecond, time.Millisecond)
}

func TestQlogFloorStopsALiveConnection(t *testing.T) {
	var free atomic.Int64
	free.Store(1000)
	freeBytes := func(string) (int64, error) { return free.Load(), nil }
	w, notes := newQlogTestWriter(t, qlogConfig{minFreeBytes: 500}, qlogIO{freeBytes: freeBytes})

	trace := w.trace(context.Background(), false, qlogConnID(1))
	require.NotNil(t, trace)
	recorder := trace.AddProducer()
	requireQlogFile(t, qlogTracePath(w, 1))

	free.Store(10)
	payload := strings.Repeat("x", 4096)
	require.Eventually(t, func() bool {
		for range 300 {
			recorder.RecordEvent(qlogTestEvent{payload: payload})
		}

		return w.stopped.Load()
	}, qlogTestWait, time.Millisecond)
	_ = recorder.Close()

	assert.Equal(t, 1, notes.count("below floor"))
}

func TestQlogUnreadableFreeSpaceFailsClosed(t *testing.T) {
	broken := func(string) (int64, error) { return 0, errors.New("statfs failed") }
	w, notes := newQlogTestWriter(t, qlogConfig{}, qlogIO{freeBytes: broken})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	assert.NoFileExists(t, qlogTracePath(w, 1))
	assert.Equal(t, 1, notes.count("free space unreadable"))
}

func TestQlogEncodePanicStaysInsideTheTrace(t *testing.T) {
	w, _ := newQlogTestWriter(t, qlogConfig{}, qlogIO{})

	trace := w.trace(context.Background(), false, qlogConnID(1))
	require.NotNil(t, trace)

	recorder := trace.AddProducer()
	recorder.RecordEvent(qlogTestEvent{panics: true})
	for range 200 {
		recorder.RecordEvent(qlogTestEvent{payload: "after"})
	}
	require.NoError(t, recorder.Close())
}

func TestQlogPruneRemovesOnlyExpiredFinishedTraces(t *testing.T) {
	dir := t.TempDir()
	old := time.Now().Add(-2 * time.Hour)

	write := func(name string, age time.Time) string {
		path := filepath.Join(dir, name)
		require.NoError(t, os.WriteFile(path, make([]byte, 1000), 0o600))
		require.NoError(t, os.Chtimes(path, age, age))

		return path
	}

	expired := write("aa01_server.sqlog", old)
	foreign := write("notes.txt", old)
	client := write("aa02_client.sqlog", old)
	young := write("aa03_server.sqlog", time.Now())
	target := write("target", old)
	link := filepath.Join(dir, "aa04_server.sqlog")
	require.NoError(t, os.Symlink(target, link))

	cfg := qlogConfig{dir: dir, maxDirBytes: 5000, pruneAge: time.Hour}
	w, notes := newQlogTestWriter(t, cfg, qlogIO{})

	recordQlogTrace(w, 1, 1, "x")
	requireQlogFile(t, qlogTracePath(w, 1))
	require.Eventually(t, func() bool { return notes.count("resumed") == 1 }, qlogTestWait, time.Millisecond)

	assert.NoFileExists(t, expired)
	assert.FileExists(t, foreign)
	assert.FileExists(t, client)
	assert.FileExists(t, young)
	assert.FileExists(t, target)
	_, err := os.Lstat(link)
	require.NoError(t, err)
	assert.False(t, w.stopped.Load())
	assert.Equal(t, 1, notes.count("stopped until pruning"))
}

func TestQlogPruneLeavesSymlinksAlone(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(t.TempDir(), "target")
	require.NoError(t, os.WriteFile(target, nil, 0o600))
	link := filepath.Join(dir, "aa01_server.sqlog")
	require.NoError(t, os.Symlink(target, link))

	cfg := qlogConfig{dir: dir, maxDirBytes: 1, pruneAge: time.Nanosecond}
	w, notes := newQlogTestWriter(t, cfg, qlogIO{})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, func() bool { return notes.count("resumed") >= 1 }, qlogTestWait, time.Millisecond)

	_, err := os.Lstat(link)
	require.NoError(t, err)
}

func TestQlogPruneNeverRemovesAnOpenTrace(t *testing.T) {
	cfg := qlogConfig{maxDirBytes: 1 << 20, pruneAge: time.Nanosecond}
	w, notes := newQlogTestWriter(t, cfg, qlogIO{})

	trace := w.trace(context.Background(), false, qlogConnID(1))
	require.NotNil(t, trace)
	recorder := trace.AddProducer()
	requireQlogFile(t, qlogTracePath(w, 1))

	// Fills the directory with a second, finished trace; the cap trips and pruning runs.
	require.True(t, recordQlogTrace(w, 2, 8000, strings.Repeat("x", 200)))
	require.Eventually(t, func() bool { return notes.count("stopped until pruning") == 1 }, qlogTestWait, time.Millisecond)

	assert.FileExists(t, qlogTracePath(w, 1))
	_ = recorder.Close()
}

func TestQlogPruneRetriesWhenTheOldestTraceExpires(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "aa01_server.sqlog")
	require.NoError(t, os.WriteFile(path, make([]byte, 100), 0o600))

	cfg := qlogConfig{dir: dir, maxDirBytes: 100, pruneAge: 300 * time.Millisecond}
	w, notes := newQlogTestWriter(t, cfg, qlogIO{})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, func() bool { return notes.count("resumed") == 1 }, qlogTestWait, time.Millisecond)

	assert.NoFileExists(t, path)
	assert.False(t, w.stopped.Load())
}

func TestQlogWithoutPruneAgeNothingIsRemoved(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "aa01_server.sqlog")
	require.NoError(t, os.WriteFile(path, make([]byte, 100), 0o600))
	old := time.Now().Add(-1000 * time.Hour)
	require.NoError(t, os.Chtimes(path, old, old))

	w, notes := newQlogTestWriter(t, qlogConfig{dir: dir, maxDirBytes: 100}, qlogIO{})

	recordQlogTrace(w, 1, 1, "x")
	require.Eventually(t, w.stopped.Load, qlogTestWait, time.Millisecond)

	assert.FileExists(t, path)
	assert.Equal(t, 1, notes.count("stopped until restart"))
}

func TestQlogHeaderValuesAreRedacted(t *testing.T) {
	assert.Equal(t, "GET", redactQlogHeader(":method", "GET"))
	assert.Equal(t, "200", redactQlogHeader(":status", "200"))
	assert.Empty(t, redactQlogHeader(":path", "/secret?token=1"))
	assert.Empty(t, redactQlogHeader("Cookie", "session=1"))
}

func TestQlogRealConnectionWritesARedactedTrace(t *testing.T) {
	dir := t.TempDir()
	tracer := newQlogTracer(qlogEnv(map[string]string{
		qlogDirEnv:          dir,
		qlogMaxDirBytesEnv:  strconv.Itoa(1 << 30),
		qlogMinFreeBytesEnv: "1",
	}))
	require.NotNil(t, tracer)

	certContent, err := localhostCert.Read()
	require.NoError(t, err)
	keyContent, err := localhostKey.Read()
	require.NoError(t, err)
	cert, err := tls.X509KeyPair(certContent, keyContent)
	require.NoError(t, err)

	udp, err := net.ListenPacket("udp", "127.0.0.1:0")
	require.NoError(t, err)

	server := &http3.Server{
		TLSConfig:  http3.ConfigureTLSConfig(&tls.Config{Certificates: []tls.Certificate{cert}}),
		QUICConfig: &quic.Config{Tracer: tracer},
		Handler:    http.HandlerFunc(func(rw http.ResponseWriter, _ *http.Request) { rw.WriteHeader(http.StatusOK) }),
	}
	go func() { _ = server.Serve(udp) }()
	t.Cleanup(func() {
		_ = server.Close()
		_ = udp.Close()
	})

	transport := &http3.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, "https://"+udp.LocalAddr().String()+"/secret-path", nil)
	require.NoError(t, err)
	request.Header.Set("Cookie", "secret-cookie")

	response, err := transport.RoundTrip(request)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())
	require.NoError(t, transport.Close())

	var content string
	require.Eventually(t, func() bool {
		paths, globErr := filepath.Glob(filepath.Join(dir, "*_server.sqlog"))
		if globErr != nil || len(paths) != 1 {
			return false
		}
		data, readErr := os.ReadFile(paths[0])
		content = string(data)

		return readErr == nil && strings.Contains(content, "connection_closed")
	}, qlogTestWait, 10*time.Millisecond)

	assert.Contains(t, content, "transport:connection_started")
	assert.Contains(t, content, "http3:")
	assert.NotContains(t, content, "secret-path")
	assert.NotContains(t, content, "secret-cookie")

	paths, err := filepath.Glob(filepath.Join(dir, "*_server.sqlog"))
	require.NoError(t, err)
	info, err := os.Stat(paths[0])
	require.NoError(t, err)
	assert.Equal(t, os.FileMode(0o600), info.Mode().Perm())
}
