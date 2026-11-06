//go:build linux || darwin

package server

import "golang.org/x/sys/unix"

func qlogFreeBytes(dir string) (int64, error) {
	var stat unix.Statfs_t
	if err := unix.Statfs(dir, &stat); err != nil {
		return 0, err
	}

	return int64(stat.Bavail) * int64(stat.Bsize), nil
}
