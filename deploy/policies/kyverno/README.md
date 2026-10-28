# Optional image-signature admission enforcement

The Helm chart pins the application image by digest, but Kubernetes itself does not validate Cosign signatures. On clusters that use Kyverno, adapt `verify-patentshield-images.example.yaml` to the installed Kyverno version, the exact internal registry/repository pattern, and the customer-controlled Cosign public key, then test in audit mode before switching to enforcement. Do not apply the placeholder PEM. An equivalent signature-verifying admission controller can be used if Kyverno is not part of the target platform.

The admission policy is intended to reject unsigned or digest-mismatched PatentShield pods before scheduling. It complements (not replaces) offline verification of the signed release bundle before importing it into the private registry.
