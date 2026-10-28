{{- define "patentshield.name" -}}
patentshield
{{- end -}}
{{- define "patentshield.labels" -}}
app.kubernetes.io/name: {{ include "patentshield.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}
{{- define "patentshield.image" -}}
{{- $repository := required "Set image.repository to the internal signed image repository" .Values.image.repository -}}
{{- $digest := required "Set image.digest to a signed sha256 digest; image tags are not accepted for private deployments" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail "image.digest must match sha256:<64 lowercase hex characters>" -}}
{{- end -}}
{{ printf "%s@%s" $repository $digest }}
{{- end -}}
{{- define "patentshield.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
fsGroup: 1000
seccompProfile:
  type: RuntimeDefault
{{- end -}}
{{- define "patentshield.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}
