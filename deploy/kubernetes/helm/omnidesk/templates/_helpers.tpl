{{/*
Runner host addresses as a JSON array. An empty list must fail the render: an
egress rule with no destinations would allow the runner port to every IP.
*/}}
{{- define "omnidesk.runnerAddresses" -}}
{{- $addresses := .Values.sandboxRunner.external.addresses | default list -}}
{{- if not $addresses -}}
{{- fail "sandboxRunner.external.addresses must list the sandbox runner host IPv4 addresses" -}}
{{- end -}}
{{- range $addresses -}}
{{- if not (regexMatch "^([0-9]{1,3}\\.){3}[0-9]{1,3}$" (toString .)) -}}
{{- fail (printf "sandboxRunner.external.addresses entry %q is not an IPv4 address" (toString .)) -}}
{{- end -}}
{{- end -}}
{{- toJson $addresses -}}
{{- end -}}
