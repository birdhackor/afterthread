// The paths of the backend's /api/tools/... routes that take a value: a tool's
// name, or a job id. Every such path in the app is written here, so each value is
// percent-encoded as one path segment. Spliced into a string as it is, a name
// holding `#` or `?` cuts the path short -- `/api/tools/kb#x` asks for
// `/api/tools/kb`, the browser taking the rest for a fragment -- and one holding
// `/` or `%` addresses another path. A route's own words (`summary`, `draft`) are
// passed as segments of their own.

// `/api/tools/<name>`, then each of `segments`, every one of them encoded.
export function toolApiPath(name, ...segments) {
	return ["/api/tools", ...[name, ...segments].map(encodeURIComponent)].join(
		"/",
	);
}

// `/api/tools/jobs/<jobId>`, the id encoded.
export function toolJobApiPath(jobId) {
	return `/api/tools/jobs/${encodeURIComponent(jobId)}`;
}
