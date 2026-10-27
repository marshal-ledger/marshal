{{- define "marshal.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "marshal.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "marshal.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
A security context without its empty values, so `runAsUser: ""` or `fsGroup: null` leaves the
field out and OpenShift's restricted SCC assigns it from the namespace's range.
*/}}
{{- define "marshal.securityContext" -}}
{{- $context := dict -}}
{{- range $key, $value := . -}}
{{- if not (or (kindIs "invalid" $value) (and (kindIs "string" $value) (eq $value ""))) -}}
{{- $_ := set $context $key $value -}}
{{- end -}}
{{- end -}}
{{- toYaml $context -}}
{{- end -}}
