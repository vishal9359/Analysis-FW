package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// logger writes structured or plain lines to stderr, so stdout carries only the
// JSON report (making the CLI safe to pipe).
type logger struct {
	level  int
	asJSON bool
}

const (
	levelDebug = iota
	levelInfo
	levelWarning
	levelError
)

func parseLevel(s string) int {
	switch strings.ToUpper(s) {
	case "DEBUG":
		return levelDebug
	case "WARNING", "WARN":
		return levelWarning
	case "ERROR":
		return levelError
	default:
		return levelInfo
	}
}

func newLogger(level, format string) *logger {
	return &logger{level: parseLevel(level), asJSON: strings.ToLower(format) == "json"}
}

func (l *logger) log(lvl int, name, format string, a ...interface{}) {
	if lvl < l.level {
		return
	}
	msg := fmt.Sprintf(format, a...)
	ts := time.Now().Format("2006-01-02 15:04:05")
	if l.asJSON {
		b, _ := json.Marshal(map[string]string{"ts": ts, "level": name, "msg": msg})
		fmt.Fprintln(os.Stderr, string(b))
		return
	}
	fmt.Fprintf(os.Stderr, "%s %s %s\n", ts, name, msg)
}

func (l *logger) Info(format string, a ...interface{})  { l.log(levelInfo, "INFO", format, a...) }
func (l *logger) Error(format string, a ...interface{}) { l.log(levelError, "ERROR", format, a...) }
