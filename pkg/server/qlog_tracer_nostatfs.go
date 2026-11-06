//go:build !linux && !darwin

package server

import "errors"

// qlogFreeBytes fails, so the free-space floor fails closed and tracing stays off here.
func qlogFreeBytes(string) (int64, error) {
	return 0, errors.New("free space is not measured on this platform")
}
