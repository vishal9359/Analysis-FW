// Package fwerr defines the framework's error classes and their exit codes.
//
// The exit code tells the caller (a shell, Airflow, CI) which failure class
// occurred, so retry policy can be decided without parsing messages.
package fwerr

import "fmt"

// Exit codes. 0 is success; each error class has its own code.
const (
	ExitOK         = 0
	ExitConfig     = 2
	ExitInput      = 3
	ExitSchema     = 4
	ExitDatabase   = 5 // retryable
	ExitIntegrity  = 6
	ExitUnexpected = 1
)

// Error is a framework error carrying the exit code for its class.
type Error struct {
	Code int
	Msg  string
	Err  error
}

func (e *Error) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("%s: %v", e.Msg, e.Err)
	}
	return e.Msg
}

func (e *Error) Unwrap() error { return e.Err }

// ExitCode returns the exit code for err — ExitUnexpected for anything that is
// not a framework error, ExitOK for nil.
func ExitCode(err error) int {
	if err == nil {
		return ExitOK
	}
	for e := err; e != nil; {
		if fe, ok := e.(*Error); ok {
			return fe.Code
		}
		u, ok := e.(interface{ Unwrap() error })
		if !ok {
			break
		}
		e = u.Unwrap()
	}
	return ExitUnexpected
}

func newf(code int, err error, format string, a ...interface{}) *Error {
	return &Error{Code: code, Msg: fmt.Sprintf(format, a...), Err: err}
}

// Config reports a bad or missing configuration.
func Config(format string, a ...interface{}) *Error {
	return newf(ExitConfig, nil, format, a...)
}

// ConfigWrap is Config with an underlying cause.
func ConfigWrap(err error, format string, a ...interface{}) *Error {
	return newf(ExitConfig, err, format, a...)
}

// Input reports unusable input: a missing directory, an unparseable .pb.
func Input(format string, a ...interface{}) *Error {
	return newf(ExitInput, nil, format, a...)
}

// InputWrap is Input with an underlying cause.
func InputWrap(err error, format string, a ...interface{}) *Error {
	return newf(ExitInput, err, format, a...)
}

// Schema reports a .proto that violates the producer contract.
func Schema(format string, a ...interface{}) *Error {
	return newf(ExitSchema, nil, format, a...)
}

// SchemaWrap is Schema with an underlying cause.
func SchemaWrap(err error, format string, a ...interface{}) *Error {
	return newf(ExitSchema, err, format, a...)
}

// Database reports a database failure — the retryable class.
func Database(format string, a ...interface{}) *Error {
	return newf(ExitDatabase, nil, format, a...)
}

// DatabaseWrap is Database with an underlying cause.
func DatabaseWrap(err error, format string, a ...interface{}) *Error {
	return newf(ExitDatabase, err, format, a...)
}

// Integrity reports a reconciliation mismatch: records read != rows written.
func Integrity(format string, a ...interface{}) *Error {
	return newf(ExitIntegrity, nil, format, a...)
}
