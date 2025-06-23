#!/bin/bash

set -e

NAMESPACE=skyvern

echo "Creating namespace..."
kubectl apply -f namespace.yaml

echo "Deploying Postgres..."
kubectl apply -f postgres/postgres-secrets.yaml -n $NAMESPACE
kubectl apply -f postgres/postgres-storage.yaml -n $NAMESPACE
kubectl apply -f postgres/postgres-deployment.yaml -n $NAMESPACE
kubectl apply -f postgres/postgres-service.yaml -n $NAMESPACE

echo "Deploying Skyvern Backend..."
kubectl apply -f backend/backend-secrets.yaml -n $NAMESPACE
kubectl apply -f backend/backend-deployment.yaml -n $NAMESPACE
kubectl apply -f backend/backend-service.yaml -n $NAMESPACE

echo "Deploying Skyvern Frontend..."
kubectl apply -f frontend/frontend-secrets.yaml -n $NAMESPACE
kubectl apply -f frontend/frontend-deployment.yaml -n $NAMESPACE
kubectl apply -f frontend/frontend-service.yaml -n $NAMESPACE

echo "Deploying Ingress..."
kubectl apply -f ingress.yaml -n $NAMESPACE

echo "Deployment complete!"