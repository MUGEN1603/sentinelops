#!/bin/bash
# install-cert-manager.sh — Install cert-manager and create self-signed CA for in-cluster TLS
# Run after cluster creation: ./install-cert-manager.sh

set -e

echo "=== Installing cert-manager ==="
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml

echo "=== Waiting for cert-manager pods to be ready ==="
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=120s

echo "=== Creating self-signed ClusterIssuer ==="
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-issuer
spec:
  selfSigned: {}
EOF

echo "=== Creating CA certificate for internal TLS ==="
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: sentinelops-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: sentinelops.internal
  secretName: sentinelops-ca-secret
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned-issuer
    kind: ClusterIssuer
    group: cert-manager.io
  duration: 8760h # 1 year
  renewBefore: 720h # 30 days
EOF

echo "=== Creating Venafi/ClusterIssuer for internal services ==="
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: sentinelops-ca-issuer
spec:
  ca:
    secretName: sentinelops-ca-secret
EOF

echo "=== Verifying CA certificate ==="
kubectl wait --for=condition=ready certificate/sentinelops-ca -n cert-manager --timeout=60s
kubectl get secret sentinelops-ca-secret -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d | openssl x509 -text -noout | head -20

echo "✅ cert-manager with self-signed CA installed successfully"
echo "   ClusterIssuer 'sentinelops-ca-issuer' ready for internal TLS certificates"