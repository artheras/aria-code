//go:build unix

package main

import "syscall"

func noFollowFlag() int { return syscall.O_NOFOLLOW }
